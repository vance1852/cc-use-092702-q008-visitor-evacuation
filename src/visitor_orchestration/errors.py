"""游客承载编排服务向 API 和 CLI 暴露的稳定错误。"""


class VisitorOrchestrationError(RuntimeError):
    code = "orchestration_error"
    status = 400


class NotFound(VisitorOrchestrationError):
    code = "not_found"
    status = 404


class Conflict(VisitorOrchestrationError):
    code = "conflict"
    status = 409


class Forbidden(VisitorOrchestrationError):
    code = "forbidden"
    status = 403


class InvalidState(VisitorOrchestrationError):
    code = "invalid_state"
    status = 409


class ValidationFailed(VisitorOrchestrationError):
    code = "validation_failed"
    status = 422
