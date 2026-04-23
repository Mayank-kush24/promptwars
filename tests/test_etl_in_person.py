from io import BytesIO

import pandas as pd
import pytest

from scripts import etl_in_person


def test_join_stats():
    df_r = pd.DataFrame(
        {"user_id": ["a", "b"], "city_id": [1, 1], "display_name": ["A", "B"]}
    )
    df_s = pd.DataFrame({"user_id": ["a"], "city_id": [1], "display_name": ["A"]})
    df_r = etl_in_person._coerce_ids(etl_in_person._normalize_columns(df_r.copy()))
    df_s = etl_in_person._coerce_ids(etl_in_person._normalize_columns(df_s.copy()))
    stats = etl_in_person.build_join_stats(df_r, df_s)
    assert stats["rsvp_unique_pairs"] == 2
    assert stats["submission_unique_pairs"] == 1
    assert stats["converted_pairs"] == 1


def test_parse_rsvps_csv():
    csv = BytesIO(b"user_id,city_id,display_name\nu1,10,Alice\n")
    df = etl_in_person.parse_rsvps_csv(csv)
    assert len(df) == 1
    assert df.iloc[0]["user_id"] == "u1"
    assert int(df.iloc[0]["city_id"]) == 10


def test_validate_city_ids():
    df_r = pd.DataFrame({"user_id": ["a"], "city_id": [1]})
    df_s = pd.DataFrame({"user_id": ["a"], "city_id": [2]})
    etl_in_person.validate_city_ids_for_event(df_r, df_s, {1, 2})
    with pytest.raises(ValueError):
        etl_in_person.validate_city_ids_for_event(df_r, df_s, {1})
