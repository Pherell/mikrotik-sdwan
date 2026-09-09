"""M7: telemetry read back from every device, on a schedule.

The poller (app.telemetry.poller) reads netwatch and system/resource, which
the controller already asks every device to maintain for SLA-based steering,
and stores what they say in app.models.telemetry.Sample. Queried back through
GET /api/v1/links/{id}/series (app.api.v1.telemetry).
"""
