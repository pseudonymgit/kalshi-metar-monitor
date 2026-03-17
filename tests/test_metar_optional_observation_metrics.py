import unittest

from core.metar_monitor import _parse_nws_collection, _parse_tgftp_text


class MetarOptionalObservationMetricsTests(unittest.TestCase):
    def test_parse_nws_collection_includes_optional_environmental_metrics(self):
        payload = {
            "features": [
                {
                    "properties": {
                        "timestamp": "2026-03-03T01:00:00+00:00",
                        "stationIdentifier": "KDEN",
                        "temperature": {"value": 10.0},
                        "dewpoint": {"value": 4.0},
                        "windDirection": {"value": 270},
                        "windSpeed": {"value": 5.0},
                        "windGust": {"value": 8.0},
                        "barometricPressure": {"value": 101320.0},
                        "seaLevelPressure": {"value": 101000.0},
                        "visibility": {"value": 16093.44},
                        "ceiling": {"value": 914.4},
                        "cloudLayers": [
                            {"base": {"value": 914.4}, "amount": "BKN"},
                            {"base": {"value": 1828.8}, "amount": "OVC"},
                        ],
                        "presentWeather": [{"rawString": "-RA"}, {"rawString": "FG"}],
                    }
                }
            ]
        }

        obs = _parse_nws_collection(payload)[0]

        self.assertEqual(obs["station_id"], "KDEN")
        self.assertEqual(obs["temperature_c"], 10.0)
        self.assertEqual(obs["dewpoint_c"], 4.0)
        self.assertEqual(obs["wind_direction_deg"], 270)
        self.assertEqual(obs["wind_speed_kt"], 9.7)
        self.assertEqual(obs["wind_gust_kt"], 15.6)
        self.assertEqual(obs["altimeter_in_hg"], 29.92)
        self.assertEqual(obs["sea_level_pressure_mb"], 1010.0)
        self.assertEqual(obs["visibility_mi"], 10.0)
        self.assertEqual(obs["ceiling_ft"], 3000)
        self.assertEqual(obs["cloud_layers"], [{"base_ft": 3000, "amount": "BKN"}, {"base_ft": 6000, "amount": "OVC"}])
        self.assertEqual(obs["weather_codes"], ["-RA", "FG"])
        self.assertIsNone(obs["observation_age_seconds"])

    def test_parse_nws_collection_defaults_optional_fields_to_none_when_missing(self):
        payload = {
            "features": [
                {
                    "properties": {
                        "timestamp": "2026-03-03T01:00:00+00:00",
                        "temperature": {"value": 10.0},
                    }
                }
            ]
        }

        obs = _parse_nws_collection(payload)[0]

        self.assertIsNone(obs["dewpoint_c"])
        self.assertIsNone(obs["wind_direction_deg"])
        self.assertIsNone(obs["wind_speed_kt"])
        self.assertIsNone(obs["wind_gust_kt"])
        self.assertIsNone(obs["altimeter_in_hg"])
        self.assertIsNone(obs["sea_level_pressure_mb"])
        self.assertIsNone(obs["visibility_mi"])
        self.assertIsNone(obs["ceiling_ft"])
        self.assertIsNone(obs["cloud_layers"])
        self.assertIsNone(obs["weather_codes"])
        self.assertIsNone(obs["observation_age_seconds"])

    def test_parse_tgftp_text_sets_station_id_and_optional_metrics_default_none(self):
        parsed = _parse_tgftp_text("2026/03/03 01:00\nKDEN 030100Z 27010KT 10SM FEW060 10/04 A2992")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["station_id"], "KDEN")
        self.assertIsNone(parsed["dewpoint_c"])
        self.assertIsNone(parsed["wind_speed_kt"])

    def test_parse_tgftp_text_supports_negative_metar_temperature_token(self):
        parsed = _parse_tgftp_text("2026/03/03 01:00\nKDEN 030100Z 27010KT 10SM FEW060 M02/M05 A2992")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["temp_f"], 28.4)

    def test_parse_tgftp_text_ignores_missing_temperature_token(self):
        parsed = _parse_tgftp_text("2026/03/03 01:00\nKDEN 030100Z 27010KT 10SM FEW060 //// A2992")

        self.assertIsNone(parsed)

    def test_parse_tgftp_text_rejects_malformed_temperature_token(self):
        parsed = _parse_tgftp_text("2026/03/03 01:00\nKDEN 030100Z 27010KT 10SM FEW060 XX/YY A2992")

        self.assertIsNone(parsed)


if __name__ == "__main__":
    unittest.main()
