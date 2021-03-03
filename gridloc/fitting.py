"""Functions to compute the actual fitting of the grid onto the brain surface
"""
from scipy.optimize import basinhopping, brute, minimize
from scipy.stats import spearmanr
from numpy.linalg import norm
from numpy import arange, array, nanmax, nanmin, argmin, intersect1d, corrcoef
from logging import getLogger
from datetime import datetime
from json import dump

try:
    import mkl
except ImportError:
    mkl = None

from .geometry import search_grid
from .morphology.distance import compute_distance
from .vascular.sphere import compute_vasculature
from .construct import construct_grid
from .io import read_surf, read_surface_ras_shift, export_grid, read_volume, write_tsv
from .viz import to_html, to_div, plot_electrodes
from .ecog.plot_ecog import plot_2d

lg = getLogger(__name__)


def fitting(T1_file, dura_file, pial_file, initial, ecog, output, angio_file=None,
            angio_threshold=None, intermediate=None, correlation=None,
            ranges={}, method='simplex'):
    """Fit the brain activity onto the surface

    Parameters
    ----------
    T1_file : path
        path to T1 image (in particular, the T1.mgz from freesurfer)
    dura_file : path
        path to dura surface (for example, the smoothed pial surface)
    pial_file : path
        path to pial surface (in particular, the lh.pial or rh.pial from freesurfer)
    initial : dict
        start position for search, with fields:
            - label : label for the reference electrode
            - RAS : initial location for the reference electrode
            - rotation : degree of rotation of the grid (in degrees, 0° is roughly pointing up)
    angio_file : path or None
        path to angiogram (in NIfTI format). Optional.
    angio_threshold : float
        value to threshold the angio_file. Optional.
    intermediate : bool
        whether to save the intermediate steps of the fitting procedure
    correlation : str
        'parametric' (Pearson) or 'nonparametric' (rank)
    method : str
        'simplex', 'hopping', 'brute'
    ranges : dict of lists
        keys are x-direction, y-direction, rotation
    """
    start_time = datetime.now()

    lg.debug(f'Reading positions and computing normals of {dura_file}')
    dura = read_surf(dura_file)
    lg.debug(f'Reading positions of {pial_file}')
    pial = read_surf(pial_file, normals=False)
    ras_shift = read_surface_ras_shift(T1_file)

    if angio_file is not None and angio_file:
        lg.debug(f'Reading angiogram from {angio_file} and thresholding at {angio_threshold}')
        angio = read_volume(angio_file, angio_threshold)
        angio['pos'] -= ras_shift
    else:
        angio = None

    ref_label = initial['label']
    init_rot = initial['rotation']
    init_ras = array(initial['RAS'])
    init_vert = argmin(norm(dura['pos'] + ras_shift - init_ras, axis=1))
    vert_dist = norm(init_ras - ras_shift - dura['pos'][init_vert])

    lg.debug(f'Target RAS: {init_ras}, vertex #{init_vert} RAS: {dura["pos"][init_vert]} (distance = {vert_dist:0.3}mm)')
    lg.info(f'Starting position for {ref_label} is vertex #{init_vert} with orientation {initial["rotation"]}')

    if intermediate is not None and intermediate:
        intermediate = output / 'steps'
        intermediate.mkdir(exist_ok=True)

    minimizer_args = (
        dura,
        init_vert,
        ref_label,
        ecog,
        pial,
        angio,
        intermediate,
        correlation,
        )

    if method == 'simplex':
        m = fitting_simplex(minimizer_args, init_rot, ranges)
        best_fit = m.x

    elif method == 'hopping':
        m = fitting_hopping(minimizer_args, init_rot)
        best_fit = m.x

    elif method == 'brute':
        m = fitting_brute(minimizer_args, init_rot, ranges)
        best_fit = m[0]

    end_time = datetime.now()
    comp_dur = (end_time - start_time).total_seconds()
    lg.debug(f'Model fitting took {comp_dur:1.0f}s')

    # create grid with best values
    x, y, rotation = best_fit
    model = corr_ecog_model(best_fit, *minimizer_args, final=True)
    lg.info(f'Best fit at {x: 8.3f}mm {y: 8.3f}mm {rotation: 8.3f}° (vert{model["vert"]: 6d}) = {model["cc"]: 8.3f} (vascular contribution: {model["percent_vasc"]:.2f}%)')

    output = output / ('bestfit_' + start_time.strftime('%Y%m%d_%H%M%S'))
    output.mkdir(parents=True)

    grid_file = output / 'ecog'
    fig = plot_electrodes(pial, model['grid'], ecog['ecog'])
    to_html([to_div(fig), ], grid_file)
    lg.debug(f'Exported merged model to {grid_file}')

    grid_file = output / 'electrodes'
    write_tsv(model['grid']['label'], model['grid']['pos'] + ras_shift, grid_file)
    lg.debug(f'Exported electrodes to {grid_file} (coordinates in MRI volume space, not mesh space)')

    grid_file = output / 'morphology'
    fig0 = plot_2d(model['morpho'], 'morphology')
    fig1 = plot_electrodes(pial, model['grid'], model['morpho']['morphology'])
    to_html([to_div(fig0), to_div(fig1)], grid_file)

    if model['vasc'] is not None:
        grid_file = output / 'vascular'
        fig0 = plot_2d(model['vasc'], 'vasculature')
        fig1 = plot_electrodes(pial, model['grid'], model['vasc']['vasculature'])
        to_html([to_div(fig0), to_div(fig1)], grid_file)
        lg.debug(f'Exported vascular to {grid_file}')

        merged = (model['percent_vasc'] * normalize(model['vasc']['vasculature']) + (100 - model['percent_vasc']) * normalize(model['morpho']['morphology'])) / 100
        grid_file = output / 'merged'
        fig = plot_electrodes(pial, model['grid'], merged)
        to_html([to_div(fig), ], grid_file)
        lg.debug(f'Exported merged model to {grid_file}')

    out = {
        'ref_label': ref_label,
        'surface': str(dura_file),
        'vertex': int(model['vert']),
        'pos': list(dura['pos'][model['vert'], :] + ras_shift),
        'normals': list(dura['pos_norm'][model['vert'], :]),
        'rotation': rotation,
        'percent_vasculature': model['percent_vasc'],
        'r2': model['cc'],
        'duration': comp_dur,
        }
    results_file = output / 'results.json'
    with results_file.open('w') as f:
        dump(out, f, indent=2)


def corr_ecog_model(x0, dura, ref_vert, ref_label, ecog, pial, angio=None,
                    intermediate=None, correlation=None, final=False):
    """Main model to minimize

    """
    x, y, rotation = x0
    start_vert = search_grid(dura, ref_vert, x, y)
    if not final:
        lg.debug(f'{x0[0]: 8.3f}mm {x0[1]: 8.3f}mm {x0[2]: 8.3f}° (vert{start_vert: 6d}) = ')

    grid = construct_grid(dura, start_vert, ref_label, ecog['label'], rotation=rotation)

    morpho = compute_distance(grid, pial)

    if angio is not None and angio is not False:
        vasc = compute_vasculature(grid, angio)
        e, m, v = match_labels(ecog, morpho, vasc)

    else:
        e, m = match_labels(ecog, morpho)
        v = None

    i, cc = compare_models(e, m, v, correlation=correlation)
    if not final:
        lg.debug(' ' * 45 + f'{cc: 8.3f} (vascular contribution: {100 * (1 - i):.2f}%)')

    if not final and intermediate is not None and intermediate:
        grid_file = intermediate / f'vert{start_vert}_rot{rotation:06.3f}'
        _export_results(grid_file, grid, morpho)

    if final:
        return {
            'vert': start_vert,
            'grid': grid,
            'morpho': morpho,
            'vasc': vasc,
            'percent_vasc': 100 * (1 - i),
            'cc': cc,
            }

    else:
        return cc


def compare_models(E, M, V=None, correlation='parametric'):

    E = normalize(E)
    M = normalize(M)

    if V is not None:
        V = normalize(V)
        WEIGHTS = arange(0, 1.1, 0.1)
    else:
        V = 0
        WEIGHTS = [1, ]

    x = []
    for weight in WEIGHTS:
        prediction = weight * M + (1 - weight) * V

        if correlation == 'parametric':
            c = corrcoef(E, prediction)[0, 1]
        else:
            c = spearmanr(E, prediction).correlation

        x.append(c)

    x = array(x)
    i = argmin(x)

    return WEIGHTS[i], x[i]


def corrcoef_match(ecog, estimate, field='morphology'):
    """correlation but make sure that the labels match
    """
    good = ecog['label'][ecog['good']]
    ecog_id = intersect1d(ecog['label'], good, return_indices=True)[1]
    a = ecog['ecog'].flatten('C')[ecog_id]

    estimate_id = intersect1d(estimate['label'], good, return_indices=True)[1]
    b = estimate[field].flatten('C')[estimate_id]

    return corrcoef(a, b)[0, 1]


def normalize(x):
    return (x - nanmin(x)) / (nanmax(x) - nanmin(x))


def print_results(x0, res, accept):
    lg.info(f'Done: {x0[0]: 8.3f}mm {x0[1]: 8.3f}mm {x0[2]: 8.3f}° = {res: 8.3f}')


def match_labels(ecog, *args):
    """make sure that that the values are in the same order as the labels in
    ecog, also getting rid of bad channels"""

    good = ecog['label'][ecog['good']]
    ecog_id = intersect1d(ecog['label'], good, return_indices=True)[1]
    a = ecog['ecog'].flatten('C')[ecog_id]
    output = [a, ]

    for estimate in args:
        field = (set(estimate.dtype.names) - {'label', }).pop()
        estimate_id = intersect1d(estimate['label'], good, return_indices=True)[1]
        b = estimate[field].flatten('C')[estimate_id]
        output.append(b)

    return output


def fitting_simplex(minimizer_args, rotation, ranges):

    simplex = array([
        [-ranges['x'][0], -ranges['y'][0], rotation - ranges['rotation'][0]],
        [ranges['x'][0], -ranges['y'][0], rotation - ranges['rotation'][0]],
        [-ranges['x'][0], ranges['y'][0], rotation - ranges['rotation'][0]],
        [-ranges['x'][0], -ranges['y'][0], rotation + ranges['rotation'][0]],
        ])

    m = minimize(
        corr_ecog_model,
        array([0, 0, 0]),
        method='Nelder-Mead',
        args=minimizer_args,
        options=dict(
            maxiter=100,
            initial_simplex=simplex,
            xatol=0.5,
            fatol=0.05,
            ),
        )

    return m


def fitting_hopping(minimizer_args, rotation):

    res = basinhopping(
        corr_ecog_model,
        array([0, 0, rotation]),
        niter=100,
        T=0.5,
        stepsize=5,
        interval=10,
        callback=print_results,
        minimizer_kwargs=dict(
            method='Nelder-Mead',
            args=minimizer_args,
            options=dict(
                maxiter=10,
                maxfev=10,
                ),
        )
    )
    return res


def fitting_brute(minimizer_args, rotation, brute_ranges):

    # make sure that the last point is included
    for k, v in brute_ranges.items():
        if len(brute_ranges[k]) == 2:
            brute_ranges[k].append(1)
        brute_ranges[k][1] += brute_ranges[k][2]

    ranges = (
        slice(*brute_ranges['x']),
        slice(*brute_ranges['y']),
        slice(*brute_ranges['rotation']),
        )

    if mkl is not None:
        mkl.set_num_threads(2)

    res = brute(
        corr_ecog_model,
        ranges,
        args=minimizer_args,
        disp=True,
        workers=-1,
        full_output=True,
        finish=None,
        )

    return res


def _export_results(grid_file, grid, morpho):

    export_grid(grid['grid'], grid_file)

    image_file = grid_file.with_suffix('.html')
    fig = plot_2d(morpho, 'morphology')
    to_html([to_div(fig), ], image_file)
