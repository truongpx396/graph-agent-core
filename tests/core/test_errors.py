"""Tests for app/core/errors.py's canonical error envelope."""
from app.core.errors import AUTH_ERRORS, ERROR_KINDS, ErrorCode, ErrorEnvelope


class TestErrorEnvelope:
    def test_to_dict_is_json_safe(self):
        env = ErrorEnvelope(code=ErrorCode.TIMEOUT, message="took too long")
        d = env.to_dict()
        assert d == {"code": "timeout", "message": "took too long", "details": {"kind": "failed"}}
        assert isinstance(d["code"], str)  # not the Enum member itself

    def test_details_round_trips(self):
        env = ErrorEnvelope(
            code=ErrorCode.COST_CEILING_EXCEEDED,
            message="over budget",
            details={"limit_usd": 1.0, "spent_usd": 1.5},
        )
        assert env.to_dict()["details"] == {"limit_usd": 1.0, "spent_usd": 1.5, "kind": "blocked"}

    def test_every_code_says_what_kind_it_is_except_a_cancel_and_the_front_doors_own(self):
        """A new code must be classified (specs/025): a client renders `blocked` neutrally and only `failed` as an error. The front door answers four codes
        too, and the API must answer exactly as it does, so those carry no kind."""
        unclassified = {code for code in ErrorCode if code not in ERROR_KINDS}

        assert unclassified == {ErrorCode.CANCELLED, *(code for code in AUTH_ERRORS if code is not ErrorCode.INTERNAL)}
        assert ErrorEnvelope(ErrorCode.CANCELLED, "Stopped.").to_dict()["details"] is None
        assert ErrorEnvelope(ErrorCode.WORKER_UNREACHABLE, "No worker.").to_dict()["details"] == {"kind": "lost"}

    def test_every_code_is_a_plain_string_value(self):
        """Codes are meant to be switched on by an integrating caller —
        each must be a stable, lowercase, plain string, not an
        implementation-detail repr."""
        for code in ErrorCode:
            assert code.value == code.value.lower()
            assert " " not in code.value
