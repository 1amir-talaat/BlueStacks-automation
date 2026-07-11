"""FastAPI entry point for the local fleet service."""

from __future__ import annotations

import secrets

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from fleet_service.balance_monitor import BalanceMonitor
from fleet_service.config import FleetSettings, get_settings
from fleet_service.schemas import (
    BalanceScanRequest,
    BalanceScanResult,
    FleetInstance,
    FleetSnapshot,
    ProviderActionResult,
)
from fleet_service.service import FleetProbeService


def require_token(
    authorization: str | None = Header(default=None),
    settings: FleetSettings = Depends(get_settings),
) -> None:
    expected = settings.token.strip()
    supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Fleet service authentication failed.",
        )


def create_app() -> FastAPI:
    app = FastAPI(title="BlueStacks Fleet Service", version="0.1.0", docs_url=None)
    app.state.balance_monitor = BalanceMonitor(get_settings())

    @app.get("/v1/health")
    def health(_: None = Depends(require_token)) -> dict:
        return {"status": "ok"}

    @app.get("/v1/instances", response_model=FleetSnapshot)
    def list_instances(
        _: None = Depends(require_token),
        settings: FleetSettings = Depends(get_settings),
    ) -> FleetSnapshot:
        return FleetProbeService(settings).snapshot()

    @app.get("/v1/instances/{instance_id}", response_model=FleetInstance)
    def get_instance(
        instance_id: str,
        _: None = Depends(require_token),
        settings: FleetSettings = Depends(get_settings),
    ) -> FleetInstance:
        snapshot = FleetProbeService(settings).snapshot()
        for instance in snapshot.instances:
            if instance.instance_id == instance_id:
                return instance
        raise HTTPException(status_code=404, detail="BlueStacks instance not found.")

    @app.post(
        "/v1/instances/{instance_id}/providers/{provider_key}/launch",
        response_model=ProviderActionResult,
    )
    def launch_provider(
        instance_id: str,
        provider_key: str,
        _: None = Depends(require_token),
        settings: FleetSettings = Depends(get_settings),
    ) -> ProviderActionResult:
        try:
            return FleetProbeService(settings).launch_provider(instance_id, provider_key)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/balances")
    def cached_balances(
        request: Request,
        _: None = Depends(require_token),
    ) -> dict:
        monitor = request.app.state.balance_monitor
        return {"readings": monitor.cached_balances(), "monitor": monitor.status()}

    @app.post("/v1/balances/scan", response_model=BalanceScanResult)
    def scan_balances(
        payload: BalanceScanRequest,
        request: Request,
        _: None = Depends(require_token),
    ) -> BalanceScanResult:
        try:
            return request.app.state.balance_monitor.scan_until_eligible(payload.threshold)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app


app = create_app()
