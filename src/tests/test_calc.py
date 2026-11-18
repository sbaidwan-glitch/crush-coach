"""The coach's calculator: right answers, and nothing but arithmetic gets through."""

import math

import pytest

import calc


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("1 + 2 * 3", 7),
        ("(1 + 2) * 3", 9),
        ("2 ** 10", 1024),
        ("2 ^ 3", 8),  # ^ is a power here, as on a calculator
        ("0.030 / 0.050", 0.6),
        ("-5 + 2", -3),
        ("10 // 3", 3),
        ("10 % 4", 2),
        ("pi * 0.25 ** 2", math.pi * 0.0625),
        ("sqrt(16) + abs(-2)", 6),
        ("max(0.03, 0.05)", 0.05),
        ("sin(radians(30))", 0.5),
        ("0.5 × 4", 2),
    ],
)
def test_arithmetic(expression, expected):
    assert calc.evaluate(expression) == pytest.approx(expected)


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "   ",
        "__import__('os').system('echo hi')",
        "open('/etc/passwd')",
        "().__class__",
        "x + 1",  # an unknown name
        "'a' * 3",  # a string
        "True + 1",
        "[1, 2][0]",
        "lambda: 1",
        "9 ** 9 ** 9",
        "10 ** 1000",
        "1 / 0",
        "sqrt(-1)",
        "1 +",
        "2 * (3",
        "1" * 300,
        "sqrt(2, 3)",
        "round(2.5, ndigits=1)",  # keyword arguments are not allowed
    ],
)
def test_anything_but_arithmetic_is_refused_with_a_plain_message(expression):
    with pytest.raises(calc.CalcError) as caught:
        calc.evaluate(expression)

    assert str(caught.value)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0"),
        (0.6, "0.6"),
        (0.0300000001, "0.03"),
        (1024, "1024"),
        (3.14159265, "3.1416"),
        (1234567.89, "1.235e+06"),
        (0.00001234, "1.234e-05"),
        (2.5, "2.5"),
    ],
)
def test_results_are_short(value, text):
    assert calc.format_result(value) == text
