import sys
from pathlib import Path

STREAMLIT_APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STREAMLIT_APP_DIR))
