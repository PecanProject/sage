"""
components package
================
UI components for the Scientist Review app: library (paper list, upload,
processing), workspace (the two-pane review page), pdf_viewer, record_row
and field_row.

Components get data only through api_client.py and keep UI state in
state.py -- never by importing pipeline.* directly.
"""
