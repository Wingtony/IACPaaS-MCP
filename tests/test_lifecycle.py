from iacpaas_mcp.lifecycle import owned_process


def test_process_fallback_requires_pid_and_unique_marker(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError("Must not inspect or kill arbitrary processes")
    monkeypatch.setattr("iacpaas_mcp.lifecycle.subprocess.run",forbidden)
    for record in [{},{"pid":123},{"pid":123,"instance_id":""},{"pid":True,"instance_id":"a"*32},[],{"pid":-1,"instance_id":"a"*32}]:
        assert not owned_process(record)
