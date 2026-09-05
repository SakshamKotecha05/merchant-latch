from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

from acsa.adapters.postgres.models import AuditEvent, OutboxJob
from acsa.adapters.postgres.outbox import PostgresOutboxStore

pytestmark = pytest.mark.integration


async def test_undispatched_job_is_recovered_and_marked_once(session_factory) -> None:  # type: ignore[no-untyped-def]
    store = PostgresOutboxStore(session_factory)
    job_id = await store.enqueue(
        job_type="phase0_probe",
        aggregate_type="phase0_probe",
        aggregate_id="probe_01",
        payload={"probe": "dispatch_recovery"},
    )

    first_sweep = await store.pending_dispatch_ids(limit=10)
    await store.mark_dispatched(job_id)
    second_sweep = await store.pending_dispatch_ids(limit=10)

    assert first_sweep == [job_id]
    assert second_sweep == []

    async with session_factory() as session:
        job = await session.scalar(select(OutboxJob).where(OutboxJob.id == job_id))
    assert job is not None
    assert job.dispatched_at is not None


async def test_job_claim_is_exclusive_and_completion_is_monotonic(session_factory) -> None:  # type: ignore[no-untyped-def]
    store = PostgresOutboxStore(session_factory)
    job_id = await store.enqueue(
        job_type="phase0_probe",
        aggregate_type="phase0_probe",
        aggregate_id="probe_02",
        payload={"probe": "claim"},
    )

    first_claim = await store.claim(job_id=job_id, worker_id="worker_a")
    second_claim = await store.claim(job_id=job_id, worker_id="worker_b")
    completed = await store.complete(job_id=job_id, worker_id="worker_a")
    claim_after_completion = await store.claim(job_id=job_id, worker_id="worker_c")

    assert first_claim.state.value == "claimed"
    assert first_claim.job is not None
    assert first_claim.job.id == job_id
    assert second_claim.state.value == "leased"
    assert completed is True
    assert claim_after_completion.state.value == "completed"

    async with session_factory() as session:
        job = await session.scalar(select(OutboxJob).where(OutboxJob.id == job_id))
    assert job is not None
    assert job.completed_at is not None
    assert job.locked_by is None
    assert job.lock_expires_at is None


async def test_claim_distinguishes_an_active_lease_from_a_completed_job(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = PostgresOutboxStore(session_factory)
    job_id = await store.enqueue(
        job_type="phase0_probe",
        aggregate_type="phase0_probe",
        aggregate_id="probe_03",
        payload={"probe": "claim_state"},
    )

    first_claim = await store.claim(job_id=job_id, worker_id="worker_a")
    active_lease = await store.claim(job_id=job_id, worker_id="worker_b")
    completed = await store.complete(job_id=job_id, worker_id="worker_a")
    completed_job = await store.claim(job_id=job_id, worker_id="worker_c")

    assert first_claim.state.value == "claimed"
    assert active_lease.state.value == "leased"
    assert completed is True
    assert completed_job.state.value == "completed"


async def test_reschedule_releases_claim_and_makes_job_dispatchable_later(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = PostgresOutboxStore(session_factory)
    job_id = await store.enqueue(
        job_type="create_provider_order",
        aggregate_type="payment_attempt",
        aggregate_id="att_1",
        payload={"attempt_id": "att_1"},
    )
    await store.mark_dispatched(job_id)
    await store.claim(job_id=job_id, worker_id="worker_a")

    rescheduled = await store.reschedule(
        job_id=job_id,
        worker_id="worker_a",
        delay_seconds=2,
    )

    assert rescheduled is True
    assert await store.pending_dispatch_ids(limit=10) == []
    async with session_factory() as session:
        job = await session.get(OutboxJob, job_id)
        assert job is not None
        assert job.dispatched_at is None
        assert job.locked_by is None
        assert job.lock_expires_at is None


async def test_retryable_failure_releases_claim_and_records_stable_error(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = PostgresOutboxStore(session_factory)
    job_id = await store.enqueue(
        job_type="create_provider_order",
        aggregate_type="payment_attempt",
        aggregate_id="att_1",
        payload={"attempt_id": "att_1"},
    )
    await store.mark_dispatched(job_id)
    await store.claim(job_id=job_id, worker_id="worker_a")
    retry_at = datetime.now(UTC) + timedelta(minutes=1)

    outcome = await store.fail(
        job_id=job_id,
        worker_id="worker_a",
        error_code="provider_timeout",
        retry_at=retry_at,
    )

    assert outcome.value == "retry_scheduled"
    async with session_factory() as session:
        job = await session.get(OutboxJob, job_id)
    assert job is not None
    assert job.last_error == "provider_timeout"
    assert job.available_at == retry_at
    assert job.dispatched_at is None
    assert job.locked_by is None
    assert job.lock_expires_at is None
    assert job.dead_lettered_at is None


async def test_final_failure_dead_letters_job_and_appends_one_audit_event(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    job = OutboxJob(
        job_type="create_provider_order",
        aggregate_type="payment_attempt",
        aggregate_id="att_1",
        payload={"attempt_id": "att_1"},
        max_attempts=1,
    )
    async with session_factory() as session, session.begin():
        session.add(job)
        await session.flush()
        job_id = job.id
    store = PostgresOutboxStore(session_factory)
    await store.claim(job_id=job_id, worker_id="worker_a")

    outcome = await store.fail(
        job_id=job_id,
        worker_id="worker_a",
        error_code="provider_timeout",
        retry_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    assert outcome.value == "dead_lettered"
    async with session_factory() as session:
        stored_job = await session.get(OutboxJob, job_id)
        audit_events = list(
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.aggregate_type == "outbox_job",
                    AuditEvent.aggregate_id == str(job_id),
                )
            )
        )
    assert stored_job is not None
    assert stored_job.dead_lettered_at is not None
    assert stored_job.locked_by is None
    assert stored_job.lock_expires_at is None
    assert len(audit_events) == 1
    assert audit_events[0].event_type == "outbox.dead_lettered"
    assert audit_events[0].payload == {
        "attempt_count": 1,
        "error_code": "provider_timeout",
        "job_type": "create_provider_order",
    }


async def test_runtime_connection_cannot_act_as_owner_or_create_tables(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    async with session_factory() as session:
        runtime_role = await session.scalar(text("SELECT current_user"))
        if not runtime_role:
            pytest.fail("Runtime connection did not report an authenticated role")
        is_not_owner = await session.scalar(
            text(
                "SELECT current_user <> pg_get_userbyid(relowner) "
                "FROM pg_class WHERE oid = 'outbox_jobs'::regclass"
            )
        )
        if is_not_owner is not True:
            pytest.fail("Runtime connection must not authenticate as the schema owner")
        with pytest.raises(ProgrammingError):
            await session.execute(text("CREATE TABLE phase0_runtime_persistent_probe (id integer)"))
        await session.rollback()

    async with session_factory() as session:
        runtime_role = await session.scalar(text("SELECT current_user"))
        if not runtime_role:
            pytest.fail("Runtime connection did not report an authenticated role")
        with pytest.raises(ProgrammingError):
            await session.execute(
                text("CREATE TEMPORARY TABLE phase0_runtime_temporary_probe (id integer)")
            )
        await session.rollback()
