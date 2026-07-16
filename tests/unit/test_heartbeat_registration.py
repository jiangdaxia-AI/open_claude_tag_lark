"""Tests for HeartbeatService.start() channel scanning + job registration."""

from unittest.mock import MagicMock

from ocl.ambient.heartbeat import HeartbeatService


def test_start_registers_enabled_channels(tmp_path, monkeypatch):
    # channels_dir is a @property = data_dir / "channels", so patch data_dir
    # and create the channels/ subdir.
    channels = tmp_path / "channels"
    channels.mkdir()
    (channels / "C001").mkdir()
    (channels / "C001" / "HEARTBEAT.md").write_text(
        "---\nenabled: true\ncron: \"0 9 * * 1\"\n---\nfocus\n"
    )
    (channels / "C002").mkdir()
    (channels / "C002" / "HEARTBEAT.md").write_text(
        "---\nenabled: false\ncron: \"0 9 * * 1\"\n---\nfocus\n"
    )
    (channels / "C003").mkdir()  # no HEARTBEAT.md — skipped

    from ocl import config
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)

    scheduler = MagicMock()
    svc = HeartbeatService(
        gateway=MagicMock(tenant_id="T1"),
        scheduler=scheduler,
        get_session_lock=lambda t, c: MagicMock(),
    )
    svc.start()

    assert scheduler.add_job.call_count == 1
    call = scheduler.add_job.call_args
    assert call.kwargs["id"] == "heartbeat_C001"


def test_start_no_channels_no_jobs(tmp_path, monkeypatch):
    channels = tmp_path / "channels"
    channels.mkdir()  # empty channels dir

    from ocl import config
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    scheduler = MagicMock()
    svc = HeartbeatService(
        gateway=MagicMock(tenant_id="T1"),
        scheduler=scheduler,
        get_session_lock=lambda t, c: MagicMock(),
    )
    svc.start()
    assert scheduler.add_job.call_count == 0
