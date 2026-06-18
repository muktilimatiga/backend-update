import asyncio
import socket
import logging
from fastapi import APIRouter, HTTPException, status
from typing import Dict
from schemas.cli import TerminalResponse, StopResponse, ListResponse

logger = logging.getLogger(__name__)


# --- App Setup ---
router = APIRouter()

MAX_TERMINALS = 10  # Limit the number of concurrent terminals
running_terminals: Dict[int, asyncio.subprocess.Process] = {}

# --- Helper Functions ---
def get_free_port() -> int:
    """
    Finds and returns a free TCP port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))  # 0 tells the OS to pick a random free port
        s.listen(1)
        port = s.getsockname()[1]
    return port


async def cleanup_dead_processes():
    """
    Removes any processes that have exited from the running_terminals dict.
    """
    ports_to_remove = []
    for port, process in running_terminals.items():
        if process.returncode is not None:
            logger.info(f"Removing dead ttyd process on port {port} (PID {process.pid})")
            ports_to_remove.append(port)

    for port in ports_to_remove:
        del running_terminals[port]


def check_ttyd_available() -> bool:
    """
    Checks if ttyd command is available in the system PATH.
    """
    import shutil
    return shutil.which("ttyd") is not None


# --- API Endpoints ---

@router.post("/start_terminal", response_model=TerminalResponse, status_code=status.HTTP_201_CREATED)
async def start_terminal():
    """
    Starts a new ttyd session on a random free port.
    The terminal will run the 'bash' command.
    """
    # Check if ttyd is available
    if not check_ttyd_available():
        logger.error("ttyd command not found")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error: 'ttyd' command not found. Is it installed and in your system's PATH?"
        )

    # Clean up any dead processes
    await cleanup_dead_processes()

    # Check if we've reached the maximum number of terminals
    if len(running_terminals) >= MAX_TERMINALS:
        logger.warning(f"Maximum terminals reached ({MAX_TERMINALS})")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Maximum number of terminals ({MAX_TERMINALS}) reached. Stop some terminals before starting new ones."
        )

    port = get_free_port()
    command_to_run = "bash"  # You can change this to 'zsh', 'tmux', etc.

    ttyd_command = [
        "ttyd",
        "-p", str(port),   # Assign the free port
        "-W",              # Allow write access to the terminal
        command_to_run
    ]

    try:
        logger.info(f"Starting ttyd on port {port} with command: {' '.join(ttyd_command)}")

        # Start the ttyd process in the background
        # Redirect stdout and stderr to avoid blocking
        process = await asyncio.create_subprocess_exec(
            *ttyd_command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )

        # Store the process object
        running_terminals[port] = process

        logger.info(f"ttyd session started on port {port}, PID: {process.pid}")

        return {
            "port": port,
            "pid": process.pid,
            "command": " ".join(ttyd_command),
            "message": f"ttyd session started on port {port}. PID: {process.pid}"
        }

    except Exception as e:
        logger.error(f"Failed to start ttyd on port {port}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {str(e)}"
        )


@router.post("/stop_terminal/{port}", response_model=StopResponse)
async def stop_terminal(port: int):
    """
    Stops a running ttyd session by its port number.
    """
    if port not in running_terminals:
        logger.warning(f"Attempted to stop non-existent terminal on port {port}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No ttyd session found running on port {port}."
        )

    process = running_terminals[port]
    pid = process.pid

    try:
        logger.info(f"Stopping ttyd process on port {port}, PID: {pid}")
        process.terminate()  # Send SIGTERM

        try:
            # Wait for up to 5 seconds for graceful shutdown
            await asyncio.wait_for(process.wait(), timeout=5.0)
            logger.info(f"ttyd process on port {port} stopped gracefully")
        except asyncio.TimeoutError:
            logger.warning(f"ttyd process on port {port} did not stop gracefully, killing")
            process.kill()
            await process.wait()
            logger.info(f"ttyd process on port {port} was killed")

        # Clean up
        del running_terminals[port]

        return {
            "port": port,
            "pid": pid,
            "message": "Terminal session stopped successfully."
        }
    except Exception as e:
        logger.error(f"Error stopping terminal on port {port}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error stopping terminal on port {port}: {str(e)}"
        )


@router.get("/running_terminals", response_model=ListResponse)
async def list_running_terminals():
    """
    Lists all ttyd sessions started by this API.
    """
    # Clean up any dead processes
    await cleanup_dead_processes()

    logger.debug(f"Listing {len(running_terminals)} running terminals: {list(running_terminals.keys())}")

    return {
        "count": len(running_terminals),
        "running_ports": list(running_terminals.keys())
    }