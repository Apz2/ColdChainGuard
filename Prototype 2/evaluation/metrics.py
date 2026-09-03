"""metrics i'm computing after each scenario run."""

from __future__ import annotations

_OVERLAP_TOLERANCE_S = 1.0


def _intervals_overlap(
    detected_start: float,
    detected_end: float,
    truth_start: float,
    truth_end: float,
) -> bool:
    """checking if two time windows overlap at all."""
    return detected_start <= (truth_end + _OVERLAP_TOLERANCE_S) and truth_start <= (
        detected_end + _OVERLAP_TOLERANCE_S
    )


def detection_latency(injection_ts: float, alert_ts: float) -> float:
    """counting sim seconds from fault injection to first EXCURSION."""
    return alert_ts - injection_ts


def precision_recall(
    detected: list[tuple[float, float]],
    truth: list[tuple[float, float]],
) -> tuple[float, float]:
    """matching detections to truth windows by overlap — TP if they share any time."""
    if not detected and not truth:
        return 1.0, 1.0

    matched_truth: set[int] = set()
    true_positives = 0

    for det_start, det_end in detected:
        matched = False
        for index, (truth_start, truth_end) in enumerate(truth):
            if _intervals_overlap(det_start, det_end, truth_start, truth_end):
                matched_truth.add(index)
                matched = True
                break
        if matched:
            true_positives += 1

    false_positives = len(detected) - true_positives
    false_negatives = len(truth) - len(matched_truth)

    if true_positives + false_positives == 0:
        precision = 1.0
    else:
        precision = true_positives / (true_positives + false_positives)

    if true_positives + false_negatives == 0:
        recall = 1.0
    else:
        recall = true_positives / (true_positives + false_negatives)

    return precision, recall


def disposition_accuracy(actual: list[str], expected: list[str]) -> float:
    """seeing how many disposition labels matched what i expected."""
    if not actual and not expected:
        return 1.0
    if len(actual) != len(expected):
        pair_count = min(len(actual), len(expected))
        if pair_count == 0:
            return 0.0
        matches = sum(
            1 for index in range(pair_count) if actual[index] == expected[index]
        )
        return matches / max(len(actual), len(expected))

    matches = sum(1 for actual_value, expected_value in zip(actual, expected) if actual_value == expected_value)
    return matches / len(expected)


def data_completeness(generated: int, stored: int, gaps: int) -> float:
    """stored / generated — also tracking seq gaps separately."""
    if generated <= 0:
        return 1.0
    return stored / generated


def rejection_rate(injected: int, rejected: int) -> float:
    """how many adversarial frames the verifier is rejecting."""
    if injected <= 0:
        return 1.0
    return rejected / injected
