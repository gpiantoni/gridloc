from nibabel.freesurfer.io import write_geometry
from numpy import reshape, corrcoef, isnan, mean, std, savetxt, array
from numpy.linalg import norm
from logging import getLogger

from .vascular import calculateAngioMap
from ..io import read_ecog2d, read_surf, read_surface_ras_shift, read_elec
from .io import read_matlab
from .utils import get_initial_from_matlab
from ..fitting import fitting
from ..examine import measure_distances, measure_angles
from .compat import projectElectrodes, createGrid


lg = getLogger(__name__)


def compare_to_matlab(parameters):
    convert_neuralAct_surfaces(parameters)

    m_out = parameters['output_dir'] / 'matlab'
    m_out.mkdir(exist_ok=True)

    compare_values(parameters, m_out)

    compare_position_in_space(parameters)

    cc = compare_ecog(parameters)
    lg.info(f'Correlation of gamma activity between matlab and python: {cc:0.3f}')
    savetxt(str(m_out / 'gamma.txt'), cc, fmt='%.3f')

    cc = compare_angio(parameters)
    if cc is not None:
        savetxt(str(m_out / 'angio.txt'), cc, fmt='%.3f')
        lg.info(f'Correlation of angiogram projection between matlab and python: {cc:0.3f}')

    compare_fitting(parameters)


def compare_fitting(parameters):

    grid2d_tsv = parameters['output_dir'] / 'grid2d_labels.tsv'
    ecog_tsv = parameters['output_dir'] / 'grid2d_ecog.tsv'
    ecog2d = read_ecog2d(ecog_tsv, grid2d_tsv)

    parameters = get_initial_from_matlab(parameters)
    parameters['fit']['method'] = 'simplex'
    parameters['fit']['ranges'] = {
        'x': [-10, 10, 10],
        'y': [-10, 10, 10],
        'rotation': [-45, 10, 45],
        }
    gridv2 = fitting(
        ecog=ecog2d,
        output=parameters['output_dir'],
        **parameters['fit'])

    if parameters['matlab']['comparison']['prediction_file'] is not None:
        lg.info('Gridloc(matlab) best fit')
        gridv1 = read_elec(
            gridv2,
            parameters['matlab']['comparison']['prediction_file'])
        measure_distances(gridv1)
        measure_angles(gridv1)

        distances = norm(gridv2['pos'] - gridv1['pos'], axis=2)
        lg.info(f'Distance between best fit in v1 and best fit in v2: {mean(distances):0.3f}mm (sd {std(distances):0.3f})')


def convert_neuralAct_surfaces(parameters):

    subjectInfo = read_matlab(parameters['matlab']['input']['subjectInfo_file'])
    neuralAct = read_matlab(subjectInfo['neuralAct'])

    conversion_dir = parameters['output_dir'] / 'surfaces'
    conversion_dir.mkdir(exist_ok=True, parents=True)
    parameters['matlab']['surfaces'] = {}
    for surf_type in list(neuralAct):
        surf_file = conversion_dir / surf_type
        tri = neuralAct[surf_type]['tri'].astype(int) - 1
        write_geometry(str(surf_file), neuralAct[surf_type]['vert'], tri)
        parameters['matlab']['surfaces'][surf_type] = surf_file

    return parameters


def compare_position_in_space(parameters):
    ras_shift = read_surface_ras_shift(parameters['fit']['T1_file'])

    lg.debug('Most inferior and most superior values for these surfaces (to check alignment)')
    for surf_type in ['cortex', 'hullcortex', 'cortexcoarser']:
        surf = read_surf(parameters['matlab']['surfaces'][surf_type], ras_shift=[0, 0, 0], normals=False)
        lg.debug(f'{surf_type: >20}: [{min(surf["pos"][:, 2]): 7.3f} - {max(surf["pos"][:, 2]): 7.3f}]')

    for surf_type in ['dura_file', 'pial_file']:
        surf = read_surf(parameters['fit'][surf_type], ras_shift=ras_shift, normals=False)
        lg.debug(f'{surf_type: >20}: [{min(surf["pos"][:, 2]): 7.3f} - {max(surf["pos"][:, 2]): 7.3f}]')


def compare_ecog(parameters):

    grid2d_tsv = parameters['output_dir'] / 'grid2d_labels.tsv'
    ecog_tsv = parameters['output_dir'] / 'grid2d_ecog.tsv'
    ecog2d = read_ecog2d(ecog_tsv, grid2d_tsv)

    subjectInfo = read_matlab(parameters['matlab']['input']['subjectInfo_file'])
    gamma_mean = read_matlab(subjectInfo['gamma_mean'])

    ecog2d_orig = ecog2d.copy()
    ecog2d['ecog'] = reshape(gamma_mean, ecog2d.shape, 'F')

    i_x = ecog2d_orig['ecog'].flatten()
    i_y = ecog2d['ecog'].flatten()

    cc = corrcoef(
        i_x[~isnan(i_x) & ~isnan(i_y)],
        i_y[~isnan(i_x) & ~isnan(i_y)],
        )[0, 1]
    return cc


def compare_angio(parameters):
    subjectInfo = read_matlab(parameters['matlab']['input']['subjectInfo_file'])
    if 'angiomap_file' not in parameters['matlab']['comparison'] or parameters['matlab']['comparison']['angiomap_file'] is None:
        lg.warning('No angiomap to compare to. Skipping')
        return

    cortex = read_surf(parameters['matlab']['surfaces']['cortex'], ras_shift=[0, 0, 0], normals=True)

    [angioMap, normAngio] = calculateAngioMap(subjectInfo, subjectInfo['Tthreshold'], subjectInfo['VoxelDepth'], plotAngio=False, cortex=cortex)
    mat_angio = read_matlab(parameters['matlab']['comparison']['angiomap_file'])

    cc = corrcoef(mat_angio['angioMap'], angioMap)[0, 1]
    return cc


def compare_values(parameters, m_out):
    lg.debug('Reading model file from matlab')
    roimat = read_matlab(parameters['matlab']['comparison']['model_file'])

    lg.debug('Reading matlab and surfaces')
    subj_info = read_matlab(parameters['matlab']['input']['subjectInfo_file'])
    gridInfo = read_matlab(parameters['matlab']['input']['gridInfo_file'])
    subjstructs = {'electrodes': gridInfo['coords_ROI']['ROI_Tangent']}
    hullcortex = read_surf(parameters['matlab']['surfaces']['hullcortex'], [0, 0, 0], normals=True)

    lg.debug('Project Electrodes')
    projectedROIpoints = projectElectrodes(hullcortex, subjstructs, 25, False)

    lg.debug('Create Grid')
    ROI = createGrid(projectedROIpoints, rotation=None, turns=None, auxDims=subj_info['dims'], subj_info=subj_info, hullcortex=hullcortex)

    lg.debug('Writing to file')
    for k in ('electrodes', 'normal', 'trielectrodes'):
        vals = compare_keys(ROI, roimat, k)
        savetxt(str(m_out / f'{k}.tsv'), vals, fmt='%.3f', delimiter='\t')


def compare_keys(ROI, roimat, key):
    v0 = []
    for i0 in range(len(ROI)):
        v1 = []
        for i1 in range(len(ROI[i0])):
            v1.append(
                norm(ROI[i0][i1]['normal'] - roimat['coords'][i0]['normal'][i1], axis=1).max()
            )
        v0.append(v1)
    return array(v0)
