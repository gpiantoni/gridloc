"""The functions in this module should have the same name and argument signature
of the Matlab functions, for compatibility"""

from numpy import array, arange, meshgrid, nanmean, isnan, dot, prod, arctan2, cross
from numpy.linalg import norm
from multiprocessing import Pool
from functools import partial

from ..geometry import calc_plane_to_axis
from .geometry import project_to_cortex
from .utils import plane_intersect, AxelRot, _apply_affine, _sort_closest_triangles


def getROI(surf, ref_vert, ROIsize=18, intElec=3):
    """generate 10x10 grid, on one plane

    TODO
    ----
    we should calculate pos and norm from
    pos -> projected position
    normal -> normal to the hullcortex
    """
    pos = surf['pos'][ref_vert, :]
    normal = surf['pos_norm'][ref_vert, :]

    # trying to replicate getROI.m (l. 69-90) but we do not consider the orientation here
    steps = arange(-ROIsize + intElec, ROIsize - intElec, intElec)
    x_mesh, y_mesh = meshgrid(steps, steps)

    ROI = []
    for x, y in zip(x_mesh.flatten(), y_mesh.flatten()):
        coords_2d = array([x, y])
        plane = calc_plane_to_axis(normal)
        target = coords_2d @ plane + pos
        ROI.append(target)

    return array(ROI)


def projectElectrodes(surf, subjstructs, normway, normUse=False, interstype='',
                      intersval=0):
    """Replicate projectElectrodes. This function is a ray-triangle intersection.
    The normal of the point is specified in `normway` if normway has three values.
    Otherwise, it computes the normal based on the direction of the points of the
    mesh which are closer than `normway`.

    Parameters
    ----------
    surf : dict with 'pos', 'tri'
        surface of the brain use to project the electrodes (it's not necessary
        to have 'tri_norm')
    subjstructs : dict with 'electrodes'
        location of the electrodes
    normway : float
        distance to use to include mesh points when computing the normal
    normUse : bool
        if true, it uses the normals of `subjstructs`, if false it recomputes them
    interstype : str
        if `''` all triangles of the model are processed; if `'fixed'`, you need
        to specify a radius `intersval`)
    intersval : float
        radius when `interstype` == `'fixed'`

    Returns
    -------
    dict with 'electrodes', 'normals', 'trielectrodes'
        for each electrode, it returns its normal (based on neighboring mesh
        vertices) and the projection onto the surface.
    """
    assert interstype in ('', 'fixed')

    normdist = normway  # if normway is scalar

    normals = []
    pints = []
    for i in range(subjstructs['electrodes'].shape[0]):
        electrode = subjstructs['electrodes'][i, :]

        if normUse:
            normal = subjstructs['normal'][i, :]
        else:
            normal = normElec(surf, electrode, normdist)

        if interstype == 'fixed':
            sorttri = _sort_closest_triangles(surf, electrode, intersval)

        else:
            sorttri = None

        # note the sign of normal is inverted
        pint = project_to_cortex(surf, electrode, -1 * normal, sorted_triangles=sorttri)[1]  # l. 198-237

        normals.append(normal)
        pints.append(pint)

    subjstructs['normal'] = array(normals)
    subjstructs['trielectrodes'] = array(pints)

    return subjstructs


def normElec(surf, electrode, normdist, NaN_as_zeros=True):
    """
    Notes
    -----
    When `normway` is a scalar, it takes the normal of the points of the mesh which are closer
    than `normway`. However, some points have a normal of (0, 0, 0) (default assigned
    if the vertex does not belong to any triangle). projectElectrodes.m includes
    those (0, 0, 0) in the calculation, but it might not be correct.
    See l. 138 (there are no NaN in normals but only (0, 0, 0)).

    To replicate the matlab behavior, make sure that `NaN_as_zeros` is True.
    """
    dvect = norm(electrode - surf['pos'], axis=1)  # l. 104-112 of projectElectrodes.m
    closevert = dvect < normdist  # l. 120 of projectElectrodes.m
    normal = surf['pos_norm'][closevert, :].mean(axis=0)  # l. 144 of projectElectrodes.m

    normals2av = surf['pos_norm'][closevert, :].copy()
    if NaN_as_zeros:
        normals2av[isnan(normals2av)] = 0
    normal = nanmean(normals2av, axis=0)

    return normal


def calcTangent(hullcortex, c, coords, dims, lngth, hemi):

    N = normElec(hullcortex, c, 25)
    d = dot(N, c)

    point = c
    normal = N
    tangPlane = coords.copy().reshape(prod(dims), 3)
    tangPlane[:, 0] = (-normal[1] * tangPlane[:, 1] - normal[2] * tangPlane[:, 2] - d) / normal[0]  # l. 19
    N = tangPlane[0, :] - tangPlane[7, :]
    M = coords[0, :] - coords[7, :]

    AngleBetweenPlanes = arctan2(norm(cross(M, N)), dot(M, N))

    Point, IntersectionPlanes = plane_intersect(array([1., 0., 0.]), coords[0, :], normal, point)

    RotMatrix = AxelRot(AngleBetweenPlanes, IntersectionPlanes, Point)
    coords_Tangent = _apply_affine(coords, RotMatrix)

    if hemi == 'r':
        coords_Tangent[:, 0] = coords_Tangent[::-1, 0]  # l. 54-65

    return coords_Tangent


def createGrid(sub, intElec=(3, 3), auxDims=(8, 16), hemi='r'):
    """Create GRID per ROI point and project on cortex
    """
    f = partial(createGrid_per_point, sub=sub, intElec=intElec, auxDims=auxDims, hemi=hemi)
    with Pool() as p:
        ROI = p.map(f, range(sub['electrodes'].shape[0]))

    return ROI


def createGrid_per_point(roi_punt, sub, intElec, auxDims, hemi):
    ROI_pos = sub['trielectrodes'][roi_punt, :]
    ROI_norm = sub['normal'][roi_punt, :]

    twoDgridElectrodes = calcCoords(ROI_pos, intElec, auxDims)
    electrodes_start = calcTangent(hullcortex, ROI_pos, twoDgridElectrodes, auxDims, prod(auxDims), hemi)

    normNormals = ROI_norm / norm(ROI_norm)

    # apply first rotation
    _transform = AxelRot(-45 / 180 * pi, normNormals, ROI_pos)
    electrodes_start = _apply_affine(electrodes_start, _transform)  # l. 44-50

    normdist = 25
    intersval = 30  # use largest range anyway

    coords = []
    rotations = range(90)
    for rotation in rotations:
        _transform = AxelRot(rotation / 180 * pi, normNormals, ROI_pos)
        new_turn = {'electrodes': _apply_affine(electrodes_start, _transform)}
        new_turn = projectElectrodes(hullcortex, new_turn, normdist, normUse=False, interstype='fixed', intersval=intersval)
        coords.append(new_turn)

    return coords
