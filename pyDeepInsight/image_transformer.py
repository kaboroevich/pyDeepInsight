import numpy as np
import torch
from sklearn.decomposition import PCA, KernelPCA
from sklearn.manifold import TSNE
from sklearn.cluster import BisectingKMeans
from scipy.spatial import ConvexHull
from scipy.spatial.distance import cdist
from scipy.optimize import linear_sum_assignment
import matplotlib.pyplot as plt
import inspect
import warnings

from .utils import asymmetric_greedy_search


class ImageTransformer:
    """Transform features to an image matrix using dimensionality reduction

    This class takes in data normalized between 0 and 1 and converts it to a
    CNN compatible 'image' matrix
    """

    DISCRETIZATION_OPTIONS = {
        'bin': 'coordinate_binning',
        'assignment': 'coordinate_optimal_assignment',
        'lsa': 'coordinate_optimal_assignment',
        'ags': 'coordinate_heuristic_assignment'
    }

    def __init__(self, feature_extractor='tsne', discretization='bin',
                 pixels=(224, 224)):
        """Generate an ImageTransformer instance

        Args:
            feature_extractor: string of value ('tsne', 'pca', 'kpca') or a
                class instance with method `fit_transform` that returns a
                2-dimensional array of extracted features.
            discretization: string of values ('bin', 'assignment'). Defines
                the method for discretizing dimensionally reduced data to pixel
                coordinates.
            pixels: int (square matrix) or tuple of ints (height, width) that
                defines the size of the image matrix.
        """
        self._fe = self._parse_feature_extractor(feature_extractor)
        self._dm = self._parse_discretization(discretization)
        self._pixels = self._parse_pixels(pixels)
        self._xrot = np.empty(0)
        self._coords = np.empty(0)

    @staticmethod
    def _parse_pixels(pixels):
        """Check and correct pixel parameter

        Args:
            pixels: int (square matrix) or tuple of ints (height, width) that
                defines the size of the image matrix.
        """
        if isinstance(pixels, int):
            pixels = (pixels, pixels)
        return pixels

    @staticmethod
    def _parse_feature_extractor(feature_extractor):
        """Validate the feature extractor value passed to the
        constructor method and return correct method

        Args:
            feature_extractor: string of value ('tsne', 'pca', 'kpca') or a
                class instance with method `fit_transform` that returns a
                2-dimensional array of extracted features.

        Returns:
            function
        """
        if isinstance(feature_extractor, str):
            warnings.warn("Defining feature_extractor as a string of 'tsne'," +
                          " 'pca', or 'kpca' is depreciated. Please provide " +
                          " a class instance", DeprecationWarning)
            fe = feature_extractor.casefold()
            if fe == 'tsne'.casefold():
                fe_func = TSNE(n_components=2, metric='cosine')
            elif fe == 'pca'.casefold():
                fe_func = PCA(n_components=2)
            elif fe == 'kpca'.casefold():
                fe_func = KernelPCA(n_components=2, kernel='rbf')
            else:
                raise ValueError(
                    f"feature_extractor '{feature_extractor}' not valid")
        elif hasattr(feature_extractor, 'fit_transform') and \
                inspect.ismethod(feature_extractor.fit_transform):
            fe_func = feature_extractor
        else:
            raise TypeError('Parameter feature_extractor is not a '
                            'string nor has method "fit_transform"')
        return fe_func

    @classmethod
    def _parse_discretization(cls, method):
        """Validate the discretization value passed to the
        constructor method and return correct function

        Args:
            method: string of value ('bin', 'assignment')

        Returns:
            function
        """
        method_name = cls.DISCRETIZATION_OPTIONS[method]
        return getattr(cls, method_name)

    @classmethod
    def coordinate_binning(cls, position, px_size):
        """Determine the pixel locations of each feature based on the overlap of
        feature position and pixel locations.

        Args:
            position: a 2d array of feature coordinates
            px_size: tuple with image dimensions

        Returns:
            a 2d array of feature to pixel mappings
        """
        scaled = cls.scale_coordinates(position, px_size)
        px_binned = np.floor(scaled).astype(int)
        # Need to move maximum values into the lower bin
        px_binned[:, 0][px_binned[:, 0] == px_size[0]] = px_size[0] - 1
        px_binned[:, 1][px_binned[:, 1] == px_size[1]] = px_size[1] - 1
        return px_binned

    @classmethod
    def assignment_preprocessing(cls, position, px_size, max_assignments):
        """Cluster features if necessary then calculate the distance
        of those clusters to all pixels.

        Args:
            position: a 2d array of feature coordinates
            px_size: tuple with image dimensions
            max_assignments: the maximum number of clusters

        Returns:
            a tuple of the distance (cost) matrix and the feature to cluster
            mappings
        """
        scaled = cls.scale_coordinates(position, px_size)
        px_centers = cls.calculate_pixel_centroids(px_size)
        # calculate distances
        if scaled.shape[0] > max_assignments:
            dist, labels = cls.clustered_cdist(scaled, px_centers,
                                               max_assignments)
        else:
            dist = cdist(scaled, px_centers, metric='euclidean')
            labels = np.arange(scaled.shape[0])
        return dist, labels

    @classmethod
    def assignment_postprocessing(cls, position, px_size, solution, labels):
        """Generate an array of feature pixel coordinates based on the
        provided solution and labels

        Args:
            position: a 2d array of feature coordinates
            px_size: tuple with image dimensions
            solution: the assignment solution
            labels: feature to assignment cluster mappings

        Returns:
            an array of feature pixel coordinates
        """
        px_assigned = np.empty(position.shape, dtype=int)
        for i in range(position.shape[0]):
            # The feature at i
            # Is mapped to the cluster j=labels[i]
            # Which is mapped to the pixel center px_centers[j]
            # Which is mapped to the pixel k = lsa[1][j]
            # For pixel k, x = k % px_size[0] and y = k // px_size[0]
            j = labels[i]
            ki = solution[j]
            xi = ki % px_size[0]
            yi = ki // px_size[0]
            px_assigned[i] = [yi, xi]
        return px_assigned

    @classmethod
    def coordinate_optimal_assignment(cls, position, px_size):
        """Determine the pixel location of each feature using a linear sum
        assignment problem solution on the Euclidean distances between the
        features and the pixels centers'

        Args:
            position: a 2d array of feature coordinates
            px_size: tuple with image dimensions

        Returns:
            a 2d array of feature to pixel mappings
        """
        # calculate distances
        k = np.prod(px_size)
        dist, labels = cls.assignment_preprocessing(position, px_size, k)
        # assignment of features/clusters to pixels
        lsa = linear_sum_assignment(dist**2)[1]
        px_assigned = cls.assignment_postprocessing(position, px_size,
                                                    lsa, labels)
        return px_assigned

    @classmethod
    def coordinate_heuristic_assignment(cls, position, px_size):
        """Determine the pixel location of each feature using a heuristic linear
        assignment problem solution on the Euclidean distances between the
        features and the pixels' centers

        Args:
            position: a 2d array of feature coordinates
            px_size: tuple with image dimensions

        Returns:
            a 2d array of feature to pixel mappings
        """
        # AGS requires asymmetric assignment so k must be less than pixels
        k = np.prod(px_size) - 1
        dist, labels = cls.assignment_preprocessing(position, px_size, k)
        # assignment of features/clusters to pixels
        lsa = asymmetric_greedy_search(dist**2, shuffle=True, minimize=True)[1]
        px_assigned = cls.assignment_postprocessing(position, px_size,
                                                    lsa, labels)
        return px_assigned

    @staticmethod
    def calculate_pixel_centroids(px_size):
        """Generate a 2d array of the centroid of each pixel

        Args:
            px_size: tuple with image dimensions

        Returns:
            a 2d array of pixel centroid locations
        """
        px_map = np.empty((np.prod(px_size), 2))
        for i in range(0, px_size[0]):
            for j in range(0, px_size[1]):
                px_map[i * px_size[0] + j] = [i, j]
        px_centroid = px_map + 0.5
        return px_centroid

    @staticmethod
    def clustered_cdist(positions, centroids, k):
        """Cluster the features to k cluster then calculate the distance from
        the clusters to the (pixel) centroids

        Args:
            positions: the location of the features
            centroids: the centre of the pixels
            k: the number of clusters to generate

        Returns:
            a tuple of the distance (cost) matrix and the feature to cluster
            mappings
        """
        kmeans = BisectingKMeans(n_clusters=k).fit(positions)
        cl_labels = kmeans.labels_
        cl_centers = kmeans.cluster_centers_
        dist = cdist(cl_centers, centroids, metric='euclidean')
        return dist, cl_labels

    def fit(self, X, y=None, plot=False):
        """Train the image transformer from the training set (X)

        Args:
            X: {array-like, sparse matrix} of shape (n_samples, n_features)
            y: Ignored. Present for continuity with scikit-learn
            plot: boolean of whether to produce a scatter plot showing the
                feature reduction, hull points, and minimum bounding rectangle

        Returns:
            self: object
        """
        # perform dimensionality reduction
        x_new = self._fe.fit_transform(X.T)
        # get the convex hull for the points
        chvertices = ConvexHull(x_new).vertices
        hull_points = x_new[chvertices]
        # determine the minimum bounding rectangle
        mbr, mbr_rot = self._minimum_bounding_rectangle(hull_points)
        # rotate the matrix
        # save the rotated matrix in case user wants to change the pixel size
        self._xrot = np.dot(mbr_rot, x_new.T).T
        # determine feature coordinates based on pixel dimension
        self._calculate_coords()
        # plot rotation diagram if requested
        if plot is True:
            plt.scatter(x_new[:, 0], x_new[:, 1], s=1, alpha=0.2)
            plt.fill(x_new[chvertices, 0], x_new[chvertices, 1],
                     edgecolor='r', fill=False)
            plt.fill(mbr[:, 0], mbr[:, 1], edgecolor='g', fill=False)
        return self

    @property
    def pixels(self):
        """The image matrix dimensions

        Returns:
            tuple: the image matrix dimensions (height, width)

        """
        return self._pixels

    @pixels.setter
    def pixels(self, pixels):
        """Set the image matrix dimension

        Args:
            pixels: int or tuple with the dimensions (height, width)
            of the image matrix

        """
        if isinstance(pixels, int):
            pixels = (pixels, pixels)
        self._pixels = pixels
        # recalculate coordinates if already fit
        if hasattr(self, '_coords'):
            self._calculate_coords()

    @staticmethod
    def scale_coordinates(coords, dim_max):
        """Transforms a list of n-dimensional coordinates by scaling them
        between zero and the given dimensional maximum

        Args:
            coords: a 2d ndarray of coordinates
            dim_max: a list of maximum ranges for each dimension of coords

        Returns:
            a 2d ndarray of scaled coordinates
        """
        data_min = coords.min(axis=0)
        data_max = coords.max(axis=0)
        std = (coords - data_min) / (data_max - data_min)
        scaled = np.multiply(std, dim_max)
        return scaled

    def _calculate_coords(self):
        """Calculate the matrix coordinates of each feature based on the
        pixel dimensions.
        """
        px_coords = self._dm(self._xrot, self._pixels)
        self._coords = px_coords

    def transform(self, X, img_format='rgb', empty_value=0):
        """Transform the input matrix into image matrices

        Args:
            X: {array-like, sparse matrix} of shape (n_samples, n_features)
                where n_features matches the training set.
            img_format: The format of the image matrix to return.
                'scalar' returns an array of shape (N, H, W). 'rgb' returns
                a numpy.ndarray of shape (N, H, W, 3) that is compatible with
                PIL. 'pytorch' returns a torch.tensor of shape (N, 3, H, W).
            empty_value: numeric value to fill elements where no features are
                mapped. Default = 0.

        Returns:
            A list of n_samples numpy matrices of dimensions set by
            the pixel parameter
        """
        unq, idx, cnt = np.unique(self._coords, return_inverse=True,
                                  return_counts=True, axis=0)
        img_matrix = np.zeros((X.shape[0],) + self._pixels)
        if empty_value != 0:
            img_matrix[:] = empty_value
        for i, c in enumerate(unq):
            img_matrix[:, c[0], c[1]] = X[:, np.where(idx == i)[0]].mean(axis=1)

        if img_format == 'rgb':
            img_matrix = self._mat_to_rgb(img_matrix)
        elif img_format == 'scalar':
            pass
        elif img_format == 'pytorch':
            img_matrix = self._mat_to_pytorch(img_matrix)
        else:
            raise ValueError(f"'{img_format}' not accepted for img_format")
        return img_matrix

    def fit_transform(self, X, **kwargs):
        """Train the image transformer from the training set (X) and return
        the transformed data.

        Args:
            X: {array-like, sparse matrix} of shape (n_samples, n_features)

        Returns:
            A list of n_samples numpy matrices of dimensions set by
            the pixel parameter
        """
        self.fit(X)
        return self.transform(X, **kwargs)

    def inverse_transform(self, img):
        """Transform an image layer back to its original space.
            Args:
                img:

            Returns:
                A list of n_samples numpy matrices of dimensions set by
                the pixel parameter
        """
        if img.ndim == 2 and img.shape == self._pixels:
            X = img[self._coords[:, 0], self._coords[:, 1]]
        elif img.ndim == 3 and img.shape[-2:] == self._pixels:
            X = img[:, self._coords[:, 0], self._coords[:, 1]]
        elif img.ndim == 3 and img.shape[0:2] == self._pixels:
            X = img[self._coords[:, 0], self._coords[:, 1], :]
        elif img.ndim == 4 and img.shape[1:3] == self._pixels:
            X = img[:, self._coords[:, 0], self._coords[:, 1], :]
        else:
            raise ValueError((f"Expected dimensions of (B, {self._pixels[0]}, "
                              f"{self._pixels[1]}, C) where B and C are "
                              f"optional, but got {img.shape}"))
        return X

    def feature_density_matrix(self):
        """Generate image matrix with feature counts per pixel

        Returns:
            img_matrix (ndarray): matrix with feature counts per pixel
        """
        fdmat = np.zeros(self._pixels)
        np.add.at(fdmat, tuple(self._coords.T), 1)
        return fdmat

    def coords(self):
        """Get feature coordinates

        Returns:
            ndarray: the pixel coordinates for features
        """
        return self._coords.copy()

    @staticmethod
    def _minimum_bounding_rectangle(hull_points):
        """Find the smallest bounding rectangle for a set of points.

        Modified from JesseBuesking at https://stackoverflow.com/a/33619018
        Returns a set of points representing the corners of the bounding box.

        Args:
            hull_points : nx2 matrix of hull coordinates

        Returns:
            (tuple): tuple containing
                coords (ndarray): coordinates of the corners of the rectangle
                rotmat (ndarray): rotation matrix to align edges of rectangle
                    to x and y
        """

        pi2 = np.pi / 2
        # calculate edge angles
        edges = hull_points[1:] - hull_points[:-1]
        angles = np.arctan2(edges[:, 1], edges[:, 0])
        angles = np.abs(np.mod(angles, pi2))
        angles = np.unique(angles)
        # find rotation matrices
        rotations = np.vstack([
            np.cos(angles),
            -np.sin(angles),
            np.sin(angles),
            np.cos(angles)]).T
        rotations = rotations.reshape((-1, 2, 2))
        # apply rotations to the hull
        rot_points = np.dot(rotations, hull_points.T)
        # find the bounding points
        min_x = np.nanmin(rot_points[:, 0], axis=1)
        max_x = np.nanmax(rot_points[:, 0], axis=1)
        min_y = np.nanmin(rot_points[:, 1], axis=1)
        max_y = np.nanmax(rot_points[:, 1], axis=1)
        # find the box with the best area
        areas = (max_x - min_x) * (max_y - min_y)
        best_idx = np.argmin(areas)
        # return the best box
        x1 = max_x[best_idx]
        x2 = min_x[best_idx]
        y1 = max_y[best_idx]
        y2 = min_y[best_idx]
        rotmat = rotations[best_idx]
        # generate coordinates
        coords = np.zeros((4, 2))
        coords[0] = np.dot([x1, y2], rotmat)
        coords[1] = np.dot([x2, y2], rotmat)
        coords[2] = np.dot([x2, y1], rotmat)
        coords[3] = np.dot([x1, y1], rotmat)

        return coords, rotmat

    @staticmethod
    def _mat_to_rgb(mat):
        """Convert image matrix to numpy rgb format

        Args:
            mat: {array-like} (..., M, N)

        Returns:
            An numpy.ndarray (..., M, N, 3) with original values repeated
            across RGB channels.
        """

        return np.repeat(mat[..., np.newaxis], 3, axis=-1)

    @staticmethod
    def _mat_to_pytorch(mat):
        """Convert image matrix to numpy rgb format

        Args:
            mat: {array-like} (..., M, N)

        Returns:
            An torch.tensor (..., 3, M, N) with original values repeated
            across RGB channels.
        """

        return torch.from_numpy(mat).unsqueeze(1).repeat(1, 3, 1, 1)


class MRepImageTransformer:
    """Transform features to multiple image matrices using dimensionality
    reduction

    This class takes in data normalized between 0 and 1 and converts it to
    CNN compatible 'image' matrices

    """

    def __init__(self, feature_extractor, discretization='bin',
                 pixels=(224, 224)):
        """Generate an MRepImageTransformer instance

        Args:
            feature_extractor: a list of class instances with method
                `fit_transform` that returns a 2-dimensional array of extracted
                features. Alternatively a list of tuples where the first element
                is the class instance and the second is a discretization option.
            discretization: string of values ('bin', 'lsa', 'ags'). Defines
                the default method for discretizing dimensionally reduced
                data to pixel coordinates if not provided in the
                `feature_extractor` parameter
            pixels: int (square matrix) or tuple of ints (height, width) that
                defines the size of the image matrix.
        """
        self.discretization = discretization
        self._its = []
        self.pixels = pixels
        self._data = None
        for it_cfg in feature_extractor:
            it = self.initialize_image_transformer(it_cfg)
            self._its.append(it)

    def initialize_image_transformer(self, config):
        """Create an ImageTransformer instance

        Args:
            config: either a 'feature_extractor' or a tuple of
            'feature_extractor' and the discretization value

        Returns:
            an instance of ImageTransformer
        """
        if isinstance(config, (tuple, list)):
            return ImageTransformer(feature_extractor=config[0],
                                    discretization=config[1],
                                    pixels=self.pixels)
        else:
            return ImageTransformer(feature_extractor=config,
                                    discretization=self.discretization,
                                    pixels=self.pixels)

    def fit(self, X, y=None, plot=False):
        """Train the image transformer from the training set (X)

        Args:
            X: {array-like, sparse matrix} of shape (n_samples, n_features)
            y: Ignored. Present for continuity with scikit-learn
            plot: boolean of whether to produce a scatter plot showing the
                feature reduction, hull points, and minimum bounding rectangle
        """
        self._data = X.copy()
        for it in self._its:
            if plot:
                print(it._fe)
            it.fit(X, plot=plot)
            if plot:
                plt.show()

    def extend_fit(self, feature_extractor):
        """Add additional transformations to an already trained
        MRepImageTransformer instance.

        Args:
            feature_extractor: a list of class instances with method
                `fit_transform` that returns a 2-dimensional array of extracted
                features. Alternatively a list of tuples where the first element
                is the class instance and the second is a discretization option.
        """
        for it_cfg in feature_extractor:
            it = self.initialize_image_transformer(it_cfg)
            it.fit(self._data)
            self._its.append(it)

    def transform(self, X, img_format='rgb', empty_value=0,
                  collate='manifold', return_index=False):
        """Transform the input matrix into image matrices

        Args:
            X: {array-like, sparse matrix} of shape (n_samples, n_features)
                where n_features matches the training set.
            img_format: The format of the image matrix to return.
                'scalar' returns a numpy.ndarray of shape (N, H, W). 'rgb'
                returns a PIL compatible numpy.ndarray of shape (N, H, W, 3).
                'pytorch' returns a torch.tensor of shape (N, 3, H, W).
            empty_value: numeric value to fill elements where no features are
                mapped. Default = 0.
            collate: The order of the representations.
                'manifold' returns all samples sequentially for each feature
                extractor (manifold). 'sample' returns all representations for
                each sample grouped together. 'random' returns the
                representations shuffled using np.random.
            return_index: returns an array of the index in X for each
                representation.

        Returns:
            A list of n_samples * n_manifolds numpy matrices of dimensions
            set by the pixel parameter
        """
        translist = [it.transform(X, img_format, empty_value)
                     for it in self._its]
        if collate == 'manifold':
            # keep in order of manifolds
            img_matrices = np.concatenate(translist, axis=0)
            x_index = np.tile(np.arange(X.shape[0]), len(self._its))
        elif collate == 'sample':
            # reorder by sample
            img_shape = translist[0].shape[1:]
            img_matrices = np.stack(translist, axis=1).reshape(-1, *img_shape)
            x_index = np.repeat(np.arange(X.shape[0]), len(self._its))
        elif collate == 'random':
            # randomize order
            img_matrices = np.concatenate(translist, axis=0)
            x_index = np.tile(np.arange(X.shape[0]), len(self._its))
            p = np.random.permutation(x_index.shape[0])
            img_matrices = img_matrices[p]
            x_index = x_index[p]
        else:
            raise ValueError(f"collate method '{collate}' not valid")
        if img_format == 'pytorch':
            img_matrices = torch.from_numpy(img_matrices)
        if return_index:
            return img_matrices, x_index
        else:
            return img_matrices

    def fit_transform(self, X, **kwargs):
        """Train the image transformer from the training set (X) and return
        the transformed data.

        Args:
            X: {array-like, sparse matrix} of shape (n_samples, n_features)

        Returns:
            An array of n_samples * n_manifolds numpy matrices of dimensions
            set by the pixel parameter
        """
        self.fit(X)
        img_matrices = self.transform(X, **kwargs)
        return img_matrices

    @staticmethod
    def prediction_reduction(y_hat, index, reduction="mean"):
        """Reduce the prediction score for all representations of a sample
        to a single score.

        Args:
            y_hat: the representation prediction score of length n_samples *
                n_manifolds
            index: the original sample index for each representation
            reduction: specifies the reduction to apply across representations:
                'mean' or 'sum'

        Returns:
            An array of prediction score of length n_samples ordered by index.
        """
        index_set = np.unique(index)
        if reduction == 'mean':
            reduced = np.array([np.mean(y_hat[index == k]) for k in index_set])
        elif reduction == 'sum':
            reduced = np.array([np.sum(y_hat[index == k]) for k in index_set])
        else:
            raise ValueError(f"reduction method '{reduction}' not valid")
        return reduced
