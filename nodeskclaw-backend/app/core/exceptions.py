"""Unified exception handling."""

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppException(Exception):
    """Base application exception."""

    def __init__(
        self,
        code: int,
        message: str,
        status_code: int = 400,
        message_key: str | None = None,
        error_code: int | None = None,
        message_params: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ):
        self.code = code
        self.error_code = error_code if error_code is not None else code
        self.message = message
        self.message_key = message_key
        self.message_params = message_params
        self.status_code = status_code
        # 透传额外响应字段（例：422 字段级错误 → field_errors）。
        # 序列化时由 app_exception_handler 合并进顶层响应体，
        # 避免 HTTPException 的 handler 抹掉非白名单 key。
        self.extra = extra or {}


class NotFoundError(AppException):
    def __init__(self, message: str = "资源不存在", message_key: str = "errors.common.not_found"):
        super().__init__(code=40400, message=message, status_code=404, message_key=message_key)


class ForbiddenError(AppException):
    def __init__(self, message: str = "无权限", message_key: str = "errors.common.forbidden"):
        super().__init__(code=40300, message=message, status_code=403, message_key=message_key)


class PermissionDeniedError(ForbiddenError):
    """RBAC 权限决策被拒绝（参考 docs/rfcs/0001-rbac-phase1.md）。

    通常由 require_perms FastAPI 依赖在 has_perms 返回 False 时抛出；业务代码
    也可直接 raise 以模拟一次拒绝。继承 ForbiddenError 以复用 status_code=403
    和已有的全局 exception handler。
    """

    def __init__(self, perms_code: str):
        super().__init__(
            message=f"缺少权限 {perms_code}",
            message_key="errors.rbac.permission_denied",
        )


class BadRequestError(AppException):
    def __init__(
        self,
        message: str = "请求参数错误",
        message_key: str = "errors.common.bad_request",
        message_params: dict[str, str] | None = None,
    ):
        super().__init__(
            code=40000, message=message, status_code=400,
            message_key=message_key, message_params=message_params,
        )


class ConflictError(AppException):
    def __init__(
        self,
        message: str = "资源冲突",
        message_key: str = "errors.common.conflict",
        message_params: dict[str, str] | None = None,
    ):
        super().__init__(
            code=40900, message=message, status_code=409,
            message_key=message_key, message_params=message_params,
        )


class TooManyRequestsError(AppException):
    """通用 429 异常，外部 Agent 插件化接入（spec §9.5 / §6.2 step 1）使用此类型
    上抛令牌桶耗尽的情况；HTTP 状态码固定 429，message_key 走 errors.external_agent.rate_limited
    以便前端 i18n 提示。
    """

    def __init__(
        self,
        message: str = "调用频率超出限制，请稍后再试",
        message_key: str = "errors.external_agent.rate_limited",
        message_params: dict[str, str] | None = None,
    ):
        super().__init__(
            code=42900, message=message, status_code=429,
            message_key=message_key, message_params=message_params,
        )


class K8sError(AppException):
    def __init__(self, message: str = "K8s 操作失败", message_key: str = "errors.k8s.operation_failed"):
        super().__init__(code=50010, message=message, status_code=502, message_key=message_key)


class RegistryError(AppException):
    def __init__(self, message: str = "镜像仓库请求失败", message_key: str = "errors.registry.request_failed"):
        super().__init__(code=50220, message=message, status_code=502, message_key=message_key)


HTTP_STATUS_DEFAULT_CODES: dict[int, int] = {
    400: 40000,
    401: 40100,
    403: 40300,
    404: 40400,
    409: 40900,
    422: 42200,
    429: 42900,
    500: 50000,
    502: 50200,
    503: 50300,
}


def _default_code_by_status(status_code: int) -> int:
    return HTTP_STATUS_DEFAULT_CODES.get(status_code, status_code * 100)


def _normalize_http_detail(detail: Any, status_code: int) -> tuple[int, str, str]:
    default_code = _default_code_by_status(status_code)
    default_key = f"errors.http.status_{status_code}"
    default_message = "请求失败"
    if detail is None:
        return default_code, default_key, default_message

    if isinstance(detail, dict):
        error_code = detail.get("error_code") or detail.get("code") or default_code
        message_key = detail.get("message_key") or default_key
        message = detail.get("message") or detail.get("detail") or default_message
        return int(error_code), str(message_key), str(message)

    if isinstance(detail, str):
        return default_code, default_key, detail

    return default_code, default_key, default_message


def register_exception_handlers(app: FastAPI) -> None:
    """Register global exception handlers on the FastAPI app."""

    @app.exception_handler(AppException)
    async def app_exception_handler(_request: Request, exc: AppException) -> JSONResponse:
        body: dict[str, Any] = {
            "code": exc.code,
            "error_code": exc.error_code,
            "message_key": exc.message_key,
            "message": exc.message,
            "data": None,
        }
        if exc.message_params:
            body["message_params"] = exc.message_params
        if exc.extra:
            body.update(exc.extra)
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        error_code, message_key, message = _normalize_http_detail(exc.detail, exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": error_code,
                "error_code": error_code,
                "message_key": message_key,
                "message": message,
                "data": None,
            },
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception")
        return JSONResponse(
            status_code=500,
            content={
                "code": 50000,
                "error_code": 50000,
                "message_key": "errors.system.internal_error",
                "message": "服务器内部错误",
                "data": None,
            },
        )
