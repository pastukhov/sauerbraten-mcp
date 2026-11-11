import pytest
from sauer_mcp.export_journal import ExportJournal
from sauer_mcp.bridge import BridgeError


def test_replay_and_conflicting_input(tmp_path):
  journal = ExportJournal(tmp_path / "journal")
  rid = "f19acfbb-70e9-46ae-936e-11b822883028"
  first = journal.begin(rid, {"slug": "one"})
  assert first["phase"] == "accepted"
  journal.update(
    rid, phase="published", result={"artifact_id": rid, "clean_ack": "pending"}
  )
  assert journal.status(rid)["status"] == "completed"
  assert journal.begin(rid, {"slug": "one"})["phase"] == "published"
  with pytest.raises(BridgeError, match="REQUEST_ID_REUSED"):
    journal.begin(rid, {"slug": "two"})


def test_accepted_status_is_pending(tmp_path):
  journal = ExportJournal(tmp_path / "journal")
  rid = "f19acfbb-70e9-46ae-936e-11b822883028"
  journal.begin(rid, {})
  assert journal.status(rid) == {"status": "pending", "phase": "accepted"}
