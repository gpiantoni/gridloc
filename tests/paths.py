from pathlib import Path
from .utils import simulate_data

TEST_PATH = Path(__file__).resolve().parent
ANALYSIS_PATH = TEST_PATH / 'analysis'
DATA_PATH = ANALYSIS_PATH / 'data'

PARAMETERS_FILE = ANALYSIS_PATH / 'parameters.json'
SMOOTH_FILE = DATA_PATH / 'lh_smooth.pial'
PIAL_FILE = DATA_PATH / 'lh.pial'
T1_FILE = DATA_PATH / 'T1.mgz'
ECOG_FILE = DATA_PATH / 'ecog.eeg'

GENERATED_PATH = ANALYSIS_PATH / 'generated'
GENERATED_PATH.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = ANALYSIS_PATH / 'output'

data = simulate_data()
data.export(ECOG_FILE, export_format='BrainVision')
