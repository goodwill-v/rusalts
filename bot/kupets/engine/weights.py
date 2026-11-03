"""Веса SCORE. Сумма = 1.0."""

WEIGHTS: dict[str, float] = {
    "seller": 0.35,
    "price": 0.30,
    "reviews": 0.25,
    "delivery": 0.10,
}

SOFT_PENALTY_POINTS = 10.0  # штраф при неподтверждённых характеристиках

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "Веса SCORE должны суммироваться в 1.0"
