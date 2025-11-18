def compute_coherence(data):
    """
    HCOS Coherence Engine v0.1

    Input example:
    {
      "Flow": 0-1,
      "Body": 0-1,
      "Finance": 0-1,
      "LongTerm": 0-1,
      "Externalization": 0-1,
      "Overload": 0-1
    }
    """
    weights = {
        "Flow": 0.30,
        "Body": 0.15,
        "Finance": 0.20,
        "LongTerm": 0.20,
        "Externalization": 0.10,
        "Overload": -0.15
    }

    score = sum(data.get(k, 0) * w for k, w in weights.items())

    if score >= 0.75:
        state = "High"
    elif score >= 0.55:
        state = "Stable"
    elif score >= 0.35:
        state = "Fragmented"
    elif score >= 0.15:
        state = "Strained"
        # Low but not collapsed
    else:
        state = "Collapse"

    return {
        "coherence_score": round(score, 3),
        "state": state
    }
