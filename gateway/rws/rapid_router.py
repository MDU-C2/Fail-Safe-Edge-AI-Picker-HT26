import asyncio
import re
from typing import Annotated

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    status,
)

from gateway.control.router import get_control_manager
from gateway.events.manager import event_manager
from gateway.models.rws import (
    RapidModuleUpload,
    RapidStartRequest,
    RapidStatus,
    RapidTask,
)
from gateway.rws.arms_router import RAPID_NAME
from gateway.rws.client import RwsError, rws_client
from gateway.rws.errors import to_http_exception
from gateway.security.authentication import require_permission
from gateway.security.identity import Identity

MODULE_DECLARATION = re.compile(
    r"^\s*MODULE\s+([A-Za-z][A-Za-z0-9_]*)",
    re.IGNORECASE | re.MULTILINE,
)

TaskName = Annotated[
    str,
    Path(pattern=RAPID_NAME),
]

ModuleName = Annotated[
    str,
    Path(pattern=RAPID_NAME),
]

router = APIRouter(
    prefix="/robot/rapid",
    tags=["rapid"],
)


# ================================================================
# Helpers
# ================================================================

def require_lease(
    identity: Identity,
) -> None:
    manager = get_control_manager()

    if not manager.owns_control(identity.client_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Client does not own the active control lease",
        )


def require_stopped_task(
    task: str,
) -> None:
    """Fail clearly before changing modules.

    The controller only answers an unknown task or running RAPID
    with an error code, so both are checked here first.
    """

    names = [
        entry["name"]
        for entry in rws_client.tasks()
    ]

    if task not in names:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown task {task}, the controller has {', '.join(names)}",
        )

    if rws_client.execution_state() != "stopped":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="RAPID is running, stop it before changing modules",
        )


def read_status() -> RapidStatus:
    return RapidStatus(
        execution_state=rws_client.execution_state(),
        controller_state=rws_client.controller_state(),
        operation_mode=rws_client.operation_mode(),
        tasks=[
            RapidTask(
                name=task["name"],
                state=task["taskstate"],
                execution_state=task["excstate"],
                active=task["active"] == "On",
                modules=rws_client.program_modules(task["name"]),
            )
            for task in rws_client.tasks()
        ],
    )


def check_source(
    module: str,
    source: str,
) -> None:
    declaration = MODULE_DECLARATION.search(source)

    #
    # The controller names the module after the MODULE line, not
    # the file, so a mismatch would load it under another name.
    # RAPID names are case-insensitive.
    #
    if (
        declaration is None
        or declaration.group(1).lower() != module.lower()
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Source must declare MODULE {module}",
        )

    try:
        source.encode("iso-8859-1")

    except UnicodeEncodeError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Source contains characters the controller "
                f"cannot store (ISO-8859-1): {error.object[error.start:error.end]!r}"
            ),
        ) from error


async def run(
    function,
    *args,
):
    try:
        return await asyncio.to_thread(
            function,
            *args,
        )

    except RwsError as error:
        raise to_http_exception(error) from error


# ================================================================
# Endpoints
# ================================================================

@router.get(
    "",
    response_model=RapidStatus,
    summary="Get RAPID execution state and loaded program modules",
)
async def get_rapid_status(
    _: Annotated[
        Identity,
        Depends(require_permission("robot.read")),
    ],
) -> RapidStatus:
    return await run(read_status)


@router.put(
    "/tasks/{task}/modules/{module}",
    response_model=RapidStatus,
    summary="Upload and load a RAPID module into a task",
    description=(
        "Replaces a loaded module with the same name. RAPID must be "
        "stopped. The source must declare MODULE <module>."
    ),
)
async def put_rapid_module(
    task: TaskName,
    module: ModuleName,
    upload: Annotated[
        RapidModuleUpload,
        Body(),
    ],
    identity: Annotated[
        Identity,
        Depends(require_permission("robot.control")),
    ],
) -> RapidStatus:
    require_lease(identity)

    check_source(
        module,
        upload.source,
    )

    await run(
        require_stopped_task,
        task,
    )

    await run(
        rws_client.load_module,
        task,
        module,
        upload.source,
    )

    await event_manager.publish(
        "robot.rapid.loaded",
        {
            "task": task,
            "module": module,
            "by": identity.client_id,
        },
    )

    return await run(read_status)


@router.delete(
    "/tasks/{task}/modules/{module}",
    response_model=RapidStatus,
    summary="Unload a RAPID module from a task",
    description="RAPID must be stopped.",
)
async def delete_rapid_module(
    task: TaskName,
    module: ModuleName,
    identity: Annotated[
        Identity,
        Depends(require_permission("robot.control")),
    ],
) -> RapidStatus:
    require_lease(identity)

    await run(
        require_stopped_task,
        task,
    )

    loaded = await run(
        rws_client.program_modules,
        task,
    )

    if module.lower() not in (
        name.lower()
        for name in loaded
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Module {module} is not loaded in {task}",
        )

    await run(
        rws_client.unload_module,
        task,
        module,
    )

    await event_manager.publish(
        "robot.rapid.unloaded",
        {
            "task": task,
            "module": module,
            "by": identity.client_id,
        },
    )

    return await run(read_status)


@router.post(
    "/start",
    response_model=RapidStatus,
    summary="Start RAPID execution in all tasks",
    description=(
        "Starts from the current program pointer, or from main "
        "with reset_program_pointer. Needs motors on in auto mode."
    ),
)
async def start_rapid(
    identity: Annotated[
        Identity,
        Depends(require_permission("robot.control")),
    ],
    request: Annotated[
        RapidStartRequest,
        Body(),
    ] = RapidStartRequest(),
) -> RapidStatus:
    require_lease(identity)

    await run(
        rws_client.start,
        request.reset_program_pointer,
    )

    await event_manager.publish(
        "robot.rapid.started",
        {
            "reset_program_pointer": request.reset_program_pointer,
            "by": identity.client_id,
        },
    )

    return await run(read_status)


@router.post(
    "/stop",
    response_model=RapidStatus,
    summary="Stop RAPID execution in all tasks",
)
async def stop_rapid(
    identity: Annotated[
        Identity,
        Depends(require_permission("robot.control")),
    ],
) -> RapidStatus:
    require_lease(identity)

    await run(rws_client.stop)

    await event_manager.publish(
        "robot.rapid.stopped",
        {
            "by": identity.client_id,
        },
    )

    return await run(read_status)
