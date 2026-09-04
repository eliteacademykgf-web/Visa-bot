"""Разбор дат из ответа календаря.

Форма ответа VFS заранее неизвестна, поэтому разбор структурный. Здесь
проверяется, что он выдерживает разные формы — и, что важнее, что он
не выдаёт занятые дни за свободные.
"""

from datetime import date

from app.monitor.json_dates import extract, extract_from_text

SEP_14 = date(2026, 9, 14)
SEP_21 = date(2026, 9, 21)


class TestShapes:
    def test_flat_list_of_strings(self) -> None:
        assert extract({"dates": ["2026-09-14", "2026-09-21"]}).dates == (SEP_14, SEP_21)

    def test_list_of_objects(self) -> None:
        payload = [{"date": "2026-09-14"}, {"date": "2026-09-21"}]
        assert extract(payload).dates == (SEP_14, SEP_21)

    def test_nested_under_data(self) -> None:
        payload = {"data": {"calendar": {"availableDates": [{"date": "14/09/2026"}]}}}
        assert extract(payload).dates == (SEP_14,)

    def test_snake_and_camel_keys_both_work(self) -> None:
        assert extract([{"appointment_date": "2026-09-14"}]).dates == (SEP_14,)
        assert extract([{"appointmentDate": "2026-09-14"}]).dates == (SEP_14,)

    def test_duplicates_collapse(self) -> None:
        payload = [{"date": "2026-09-14"}, {"date": "14-09-2026"}]
        assert extract(payload).dates == (SEP_14,)

    def test_result_is_sorted(self) -> None:
        payload = [{"date": "2026-09-21"}, {"date": "2026-09-14"}]
        assert extract(payload).dates == (SEP_14, SEP_21)


class TestAvailabilityFlags:
    def test_explicitly_unavailable_day_is_skipped(self) -> None:
        """Главный риск разбора: выдать занятый день за свободный."""
        payload = [
            {"date": "2026-09-14", "available": False},
            {"date": "2026-09-21", "available": True},
        ]
        assert extract(payload).dates == (SEP_21,)

    def test_zero_slot_count_means_busy(self) -> None:
        payload = [
            {"date": "2026-09-14", "slots": 0},
            {"date": "2026-09-21", "slots": 3},
        ]
        assert extract(payload).dates == (SEP_21,)

    def test_string_false_is_also_unavailable(self) -> None:
        """API отдают флаги и строками — «false» это не «есть флаг, значит да»."""
        assert extract([{"date": "2026-09-14", "isAvailable": "false"}]).dates == ()

    def test_absent_flag_means_available(self) -> None:
        """Ответы, отдающие только свободные дни, флага не содержат вовсе."""
        assert extract([{"date": "2026-09-14"}]).dates == (SEP_14,)

    def test_unavailability_is_inherited_by_nested_times(self) -> None:
        payload = [
            {"date": "2026-09-14", "available": False, "slots": [{"time": "09:00"}]},
        ]
        result = extract(payload)
        assert result.dates == ()
        assert result.times == ()


class TestNoise:
    def test_service_dates_are_ignored(self) -> None:
        """«Дата генерации ответа» — не свободный день приёма."""
        payload = {"generatedAt": "2026-09-14", "dates": []}
        assert extract(payload).dates == ()

    def test_range_bounds_are_ignored(self) -> None:
        payload = {"minDate": "2026-09-01", "maxDate": "2026-12-31", "dates": []}
        assert extract(payload).dates == ()

    def test_applicant_birth_date_is_ignored(self) -> None:
        payload = {"applicant": {"dateOfBirth": "1999-05-02"}, "dates": []}
        assert extract(payload).dates == ()

    def test_unrelated_strings_do_not_become_dates(self) -> None:
        assert extract({"centre": "Almaty", "version": "8.0.29"}).dates == ()

    def test_empty_payload(self) -> None:
        assert extract({}).dates == ()
        assert not extract({}).found


class TestTimes:
    def test_times_are_collected(self) -> None:
        payload = [{"date": "2026-09-14", "slots": [{"time": "09:00"}, {"time": "14:30"}]}]
        assert extract(payload).times == ("09:00", "14:30")

    def test_non_time_strings_are_not_times(self) -> None:
        assert extract([{"date": "2026-09-14", "time": "утро"}]).times == ()


class TestFromText:
    def test_parses_json_body(self) -> None:
        assert extract_from_text('{"dates":[{"date":"2026-09-14"}]}').dates == (SEP_14,)

    def test_html_body_yields_nothing_instead_of_raising(self) -> None:
        """Вместо ответа мог прийти HTML проверки — падать здесь нельзя."""
        assert extract_from_text("<html>Just a moment...</html>").dates == ()

    def test_empty_body(self) -> None:
        assert extract_from_text("").dates == ()
