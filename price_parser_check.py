import app

CASES = [
    ("Palmolive Shampoo 350ml Passa da 2,90€ a 1,26€", 1.26),
    ("Marc O'Polo Zaino Uomo Passa da 69€ a 17,79€", 17.79),
    (
        "4 x Elmex Dentifricio Anticarie. Al supermercato il singolo flacone costa 5,59€. "
        "QUI SU AMAZON SOLO 2,69€! Sconto + Coupon in Pagina. "
        "PASSA DA 22,75€ A 10,22€",
        10.22,
    ),
]

for text, expected in CASES:
    actual = app.extract_offer_price(text)
    if actual is None or abs(actual - expected) > 0.001:
        raise SystemExit(f"PRICE_PARSER_FAIL expected={expected} actual={actual} text={text!r}")

print("PRICE_PARSER_OK")
