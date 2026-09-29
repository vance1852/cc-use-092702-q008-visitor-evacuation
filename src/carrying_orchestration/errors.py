"""承载编排服务向 API 和 CLI 暴露的稳定错误。"""


class CarryingError(RuntimeError):
    code = "carrying_error"
    status = 400


class NotFound(CarryingError):
    code = "not_found"
    status = 404


class Conflict(CarryingError):
    code = "conflict"
    status = 409


class Forbidden(CarryingError):
    code = "forbidden"
    status = 403


class InvalidState(CarryingError):
    code = "invalid_state"
    status = 409


class ValidationFailed(CarryingError):
    code = "validation_failed"
    status = 422
