from llm.narrative import _public_transparency_text


def test_report_text_and_poster_notice_share_same_deterministic_status():
    report = {
        "verified_stories": [],
        "unexplained_move_notices": [{"status": "UNEXPLAINED", "message_he": "AT&T (T) ירדה ב־10.86%; הגורם לא אומת."}],
    }
    output = _public_transparency_text("# Daily report\n", report)
    assert "לא קודמו היום סיפורים עם גורם מאומת." in output
    assert report["unexplained_move_notices"][0]["message_he"] in output


def test_no_unexplained_notice_leaves_existing_report_unchanged():
    original = "# Daily report\n"
    assert _public_transparency_text(original, {"unexplained_move_notices": []}) == original
