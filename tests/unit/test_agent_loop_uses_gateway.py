"""Regression test — agent loop uses Gateway interface, not AsyncApp."""

import inspect

from ocl.agent.loop import run_agent_loop


def test_run_agent_loop_signature_has_gateway_not_app():
    sig = inspect.signature(run_agent_loop)
    assert "gateway" in sig.parameters
    assert "app" not in sig.parameters


def test_run_agent_loop_gateway_param_is_typed_as_gateway_protocol():
    sig = inspect.signature(run_agent_loop)
    gateway_param = sig.parameters["gateway"]
    # The annotation string should reference Gateway
    assert "Gateway" in str(gateway_param.annotation)
