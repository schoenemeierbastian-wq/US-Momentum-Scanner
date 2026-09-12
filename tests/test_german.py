from spike_scanner.german import de_number, feature_label, format_feature, probability_text


def test_german_number_and_probability_formatting():
    assert de_number(1234.56, 2) == "1.234,56"
    assert probability_text(0.1234) == "12,34 %"
    assert probability_text(None) == "Noch nicht verfügbar"


def test_feature_translation_and_formatting():
    assert feature_label("ret_5m") == "Kursänderung 5 Minuten"
    assert format_feature("ret_5m", 0.1234) == "12,34 %"
