import docker
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Optional, Dict, Any
from mcp.server.fastmcp import FastMCP, Context

class DockerSandbox:
    def __init__(self):
        try:
            self.client = docker.from_env()
            print("Successfully connected to Docker daemon.")
        except docker.errors.DockerException as e:
            print(f"Error connecting to Docker: {e}")
            print("Ensure Docker Desktop or Docker Engine is running and DOCKER_HOST is set correctly.")
            raise  # Re-raise the exception to prevent server startup
        self.container = None
        self._container_id: Optional[str] = None

    def create_container(self, image: str = "alpine:latest") -> str:
        """Creates and starts a Docker container."""
        if self.container:
            print(f"Warning: Container already exists. Reusing container ID: {self._container_id}")
            return self._container_id

        print(f"Attempting to create container with image: {image}")
        try:
            print("Running container with specified parameters...")
            # Create with temporary elevated permissions to allow installation
            self.container = self.client.containers.run(
                image,
                command="tail -f /dev/null",  # Keep container running
                detach=True,
                tty=True,
                mem_limit="512m",
                cpu_quota=50000,  # Limits CPU usage (e.g., 50% of one core)
                pids_limit=100,   # Limit number of processes
                # Temporarily allow network and root access for setup
                network_mode="bridge",
                # No user restriction for install step
                read_only=False,  # Temporarily allow writes
                tmpfs={"/tmp": "rw,size=64m,noexec,nodev,nosuid"}, # Writable /tmp
            )
            self._container_id = self.container.id
            print(f"Container created successfully. ID: {self._container_id}")
            
            # Install Python within the container
            print(f"Installing Python in container {self._container_id}...")
            # Use sh -c with full environment to ensure proper installation
            install_cmd = "sh -c 'apk update && apk add --no-cache python3 py3-pip && ln -sf /usr/bin/python3 /usr/bin/python'"
            install_result = self.container.exec_run(cmd=install_cmd)
            
            if install_result.exit_code != 0:
                install_output = install_result.output.decode('utf-8', errors='replace')
                print(f"Python installation failed: {install_output}")
                raise Exception(f"Failed to install Python: {install_output}")
                
            # Debug PATH and installed binaries
            print("Checking environment after installation...")
            debug_cmd = "sh -c 'echo PATH=$PATH && ls -la /usr/bin/python* && ls -la /usr/local/bin/python*'"
            debug_result = self.container.exec_run(cmd=debug_cmd)
            debug_output = debug_result.output.decode('utf-8', errors='replace')
            print(f"Environment debug info: {debug_output}")
            
            # Try multiple approaches to verify Python installation
            print("Verifying Python installation...")
            
            # Try various paths where Python might be installed
            potential_python_paths = [
                "/usr/bin/python3",
                "/usr/bin/python",
                "/usr/local/bin/python3",
                "/usr/local/bin/python"
            ]
            
            # Check for Python binary in common locations
            python_path = None
            for path in potential_python_paths:
                test_cmd = f"test -x {path}"
                test_result = self.container.exec_run(cmd=test_cmd)
                if test_result.exit_code == 0:
                    python_path = path
                    print(f"Found Python at: {python_path}")
                    break
            
            if not python_path:
                # As a last resort, try to find Python using find
                find_cmd = "find /usr -name 'python3*' -type f -executable"
                find_result = self.container.exec_run(cmd=find_cmd)
                find_output = find_result.output.decode('utf-8', errors='replace')
                if find_output.strip():
                    python_path = find_output.splitlines()[0].strip()
                    print(f"Found Python using find: {python_path}")
                else:
                    print("Python executable not found in any common location")
                    raise Exception("Python executable not found after installation. Check container environment.")
            
            # Then check the Python version using the found path
            version_cmd = f"{python_path} --version"
            version_result = self.container.exec_run(cmd=version_cmd)
            if version_result.exit_code != 0:
                version_output = version_result.output.decode('utf-8', errors='replace')
                print(f"Python version check failed: {version_output}")
                raise Exception(f"Failed to execute Python after installation")
                
            python_version = version_result.output.decode('utf-8', errors='replace').strip()
            print(f"Python successfully installed at {python_path}, version: {python_version}")
            
            # Store the Python path for future use
            self._python_path = python_path
            
            # Execute the 'Wake up neo' message using shell as a safer option
            print(f"Executing startup command in {self._container_id}...")
            startup_cmd = "sh -c 'echo \"Wake up neo\"'"
            exec_result_startup = self.container.exec_run(
                cmd=startup_cmd,
                user="nobody",  # Now run as non-root
                workdir="/tmp"
            )
            startup_output = exec_result_startup.output.decode('utf-8', errors='replace') if exec_result_startup.output else ""
            print(f"Startup command output ({self._container_id}):\\n{startup_output}")

            return self._container_id
        except docker.errors.ImageNotFound:
            print(f"Warning: Docker image '{image}' not found locally. Attempting to pull...")
            try:
                self.client.images.pull(image)
                print(f"Image '{image}' pulled successfully. Retrying container creation...")
                # Retry creation
                return self.create_container(image)
            except docker.errors.APIError as pull_error:
                 print(f"Error pulling image '{image}': {pull_error}")
                 raise docker.errors.DockerException(f"Failed to pull image '{image}': {pull_error}") from pull_error
        except docker.errors.APIError as e:
            print(f"API error during container creation: {e}")
            raise docker.errors.DockerException(f"Failed to create container: {e}") from e
        except Exception as e: # Catch potential unexpected errors
             print(f"An unexpected error occurred during container creation: {e}")
             raise

    def run_code(self, code: str, language: str = "python") -> Dict[str, Any]:
        """Runs code in the container."""
        if not self.container:
            # Return error directly, let caller handle logging via ctx
            return {"error": "Container not initialized. Call 'initialize_sandbox' first."}

        cmd = []
        if language == "python":
            cmd = ["python3", "-c", code]  # Use python3 command
        elif language == "javascript":
             return {"error": "JavaScript execution not supported in minimal container"}
        # Add more languages as needed
        else:
            return {"error": f"Unsupported language: {language}"}

        print(f"Executing code in container {self._container_id}: {cmd}")
        try:
            # Ensure container is running
            self.container.reload()
            if self.container.status != 'running':
                 print(f"Warning: Container {self._container_id} is not running. Status: {self.container.status}. Restarting...")
                 self.container.start()

            # Execute with timeout (e.g., 10 seconds)
            # Note: exec_run doesn't have a direct timeout, manage externally or via command (e.g., `timeout` utility)
            # For simplicity here, we rely on resource limits primarily.
            exec_result = self.container.exec_run(
                cmd=cmd,
                user="nobody", # Run command as non-root
                workdir="/tmp" # Execute in the writable temp directory
            )

            output = exec_result.output.decode('utf-8', errors='replace') if exec_result.output else ""
            exit_code = exec_result.exit_code

            print(f"Execution finished. Exit code: {exit_code}. Output:\\n{output}")

            if exit_code != 0:
                # Return specific error message
                return {"exit_code": exit_code, "output": output, "error": f"Execution failed with exit code {exit_code}"}
            else:
                 # Success case
                 return {"exit_code": exit_code, "output": output}

        except docker.errors.APIError as e:
            print(f"Error executing code in container {self._container_id}: {e}")
            return {"error": f"API error during execution: {e}"}
        except Exception as e:
            print(f"Unexpected error during code execution: {e}")
            return {"error": f"Unexpected error: {e}"}


    def cleanup(self):
        """Stops and removes the container."""
        if self.container:
            container_id = self._container_id
            print(f"Cleaning up container: {container_id}")
            try:
                self.container.stop(timeout=5) # Give 5 seconds to stop gracefully
                self.container.remove(force=True) # Force remove if stop fails
                print(f"Container {container_id} stopped and removed.")
            except docker.errors.NotFound:
                 print(f"Container {container_id} already removed.")
            except docker.errors.APIError as e:
                print(f"Error during container cleanup {container_id}: {e}")
            finally:
                self.container = None
                self._container_id = None
        else:
            print("No active container to clean up.")



# Define a context structure to hold our sandbox instance
@dataclass
class SandboxContext:
    sandbox: DockerSandbox

# Lifespan manager for the sandbox
@asynccontextmanager
async def sandbox_lifespan(server: FastMCP) -> AsyncIterator[SandboxContext]:
    """Manage DockerSandbox lifecycle."""
    print("Lifespan: Initializing Docker Sandbox...")
    sandbox = DockerSandbox()
    try:
        # Yield the context containing the initialized sandbox
        print("Lifespan: Docker Sandbox initialized, yielding context.")
        yield SandboxContext(sandbox=sandbox)
    finally:
        # Cleanup when the server shuts down
        print("Lifespan: Shutting down Docker Sandbox...")
        sandbox.cleanup()

# Create the FastMCP server instance with the lifespan manager
mcp = FastMCP(
    "Docker Code Sandbox",
    lifespan=sandbox_lifespan,
    # Add dependencies if needed for deployment
    # dependencies=["docker"]
)

# --- MCP Tools Definition ---
@mcp.tool()
async def initialize_sandbox(ctx: Context, image: str = "alpine:latest") -> Dict[str, Any]:
    """
    Initializes a secure Docker container sandbox for code execution.
    Reuses existing container if already initialized.

    Args:
        image: The Docker image to use (e.g., 'python:3.12-alpine', 'node:20-slim'). Defaults to python:3.12-alpine.
    Returns:
        A dictionary containing the container ID or an error message.
    """
    sandbox: DockerSandbox = ctx.request_context.lifespan_context.sandbox
    try:
        await ctx.info(f"Attempting to initialize sandbox with image: {image}")
        container_id = sandbox.create_container(image)
        await ctx.info(f"Sandbox initialized successfully. Container ID: {container_id}")
        return {"status": "success", "container_id": container_id}
    except docker.errors.DockerException as e:
        await ctx.error(f"Docker error during sandbox initialization: {e}")
        return {"status": "error", "error_type": "DockerException", "message": str(e)}
    except Exception as e:
        await ctx.error(f"Unexpected error during sandbox initialization: {e}")
        return {"status": "error", "error_type": "UnexpectedError", "message": f"An unexpected error occurred: {e}"}


@mcp.tool()
async def execute_code(ctx: Context, code: str, language: str = "python") -> Dict[str, Any]:
    """
    Executes the given code string inside the initialized Docker sandbox.

    Args:
        code: The code string to execute.
        language: The programming language ('python', 'javascript', etc.). Defaults to python.

    Returns:
        A dictionary containing the execution result (stdout/stderr) and exit code, or an error message.
    """
    sandbox: DockerSandbox = ctx.request_context.lifespan_context.sandbox
    if not sandbox.container:
         await ctx.warning("Sandbox not initialized. Cannot execute code.")
         return {"status": "error", "error_type": "StateError", "message": "Sandbox not initialized. Call 'initialize_sandbox' first."}

    await ctx.info(f"Executing {language} code in container {sandbox._container_id}...")
    result = sandbox.run_code(code, language)

    if "error" in result:
         await ctx.error(f"Code execution failed: {result['error']}")
         return {
             "status": "error",
             "error_type": "ExecutionError",
             "message": result["error"],
             "output": result.get("output"),
             "exit_code": result.get("exit_code")
        }
    else:
        await ctx.info(f"Execution completed. Exit code: {result.get('exit_code')}")
        return {
            "status": "success",
            "exit_code": result.get("exit_code"),
            "output": result.get("output")
        }

@mcp.tool()
async def stop_sandbox(ctx: Context) -> Dict[str, str]:
    """
    Stops and removes the currently active Docker container sandbox.
    """
    sandbox: DockerSandbox = ctx.request_context.lifespan_context.sandbox
    await ctx.info("Attempting to stop and remove sandbox...")
    try:
        sandbox.cleanup()
        await ctx.info("Sandbox stopped and removed successfully.")
        return {"status": "success", "message": "Sandbox stopped and removed."}
    except Exception as e:
        await ctx.error(f"Error stopping sandbox: {e}")
        return {"status": "error", "error_type": "CleanupError", "message": f"An error occurred during cleanup: {e}"}


if __name__ == "__main__":
    # Use print here as logger isn't configured before server runs
    print("Starting server via __main__ (using FASTMCP_SERVER_HOST/PORT env vars or defaults)...")
    mcp.run()