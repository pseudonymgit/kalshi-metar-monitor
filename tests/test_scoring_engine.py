import copy
import unittest

from core.scoring_engine import score_settlement_epochs, segment_settlement_epochs


class ScoringEngineDeterminismTests(unittest.TestCase):
    def test_segments_epochs_on_successive_settlement_up(self):
        history = [
            {"id": 1, "station": "KDEN", "transition_type": "instant_up"},
            {"id": 2, "station": "KDEN", "transition_type": "settlement_up"},
            {"id": 3, "station": "KDEN", "transition_type": "instant_down"},
            {"id": 4, "station": "KDEN", "transition_type": "settlement_up"},
            {"id": 5, "station": "KDEN", "transition_type": "reversion_after_settlement"},
        ]

        epochs = segment_settlement_epochs(history)
        self.assertEqual(len(epochs), 2)

        self.assertEqual(epochs[0].settlement_transition_id, 2)
        self.assertEqual(epochs[0].transition_ids, (2, 3))

        self.assertEqual(epochs[1].settlement_transition_id, 4)
        self.assertEqual(epochs[1].transition_ids, (4, 5))

    def test_replay_reconstructible_same_transitions_same_scores(self):
        runtime_history = [
            {"id": 12, "station": "KDEN", "transition_type": "instant_down"},
            {"id": 10, "station": "KDEN", "transition_type": "settlement_up"},
            {"id": 11, "station": "KDEN", "transition_type": "instant_up"},
            {"id": 15, "station": "KDEN", "transition_type": "settlement_up"},
            {"id": 16, "station": "KDEN", "transition_type": "instant_up"},
        ]
        replay_history = list(reversed(runtime_history))

        runtime_scores = score_settlement_epochs(runtime_history)
        replay_scores = score_settlement_epochs(replay_history)

        self.assertEqual(runtime_scores, replay_scores)

    def test_scoring_does_not_mutate_transition_history(self):
        history = [
            {"id": 1, "station": "KDEN", "transition_type": "settlement_up"},
            {"id": 2, "station": "KDEN", "transition_type": "instant_up"},
        ]
        original = copy.deepcopy(history)

        score_settlement_epochs(history)

        self.assertEqual(history, original)


if __name__ == "__main__":
    unittest.main()
