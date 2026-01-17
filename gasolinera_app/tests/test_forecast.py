import pandas as pd
from gasolinera.data import sample_usage_dataframe
from gasolinera.forecast import prepare_monthly_series


def test_prepare_monthly_series():
    df = sample_usage_dataframe()
    series = prepare_monthly_series(df, 'Regular')
    assert 'ds' in series.columns and 'y' in series.columns
    assert len(series) == 12
