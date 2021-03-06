"""
Copyright 2019 Cartesi Pte. Ltd.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use
this file except in compliance with the License. You may obtain a copy of the
License at http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed
under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""

from concurrent import futures
import signal
import time
import math
import grpc
import sys
import traceback
import argparse
from grpc_reflection.v1alpha import reflection

import machine_discovery_pb2_grpc
import machine_discovery_pb2
import machine_manager_pb2_grpc
import machine_manager_pb2
import cartesi_machine_pb2
import utils
from session_registry import SessionIdException, AddressException, RollbackException

# docker graceful shutdown, raise a KeyboardInterrupt in case of SIGTERM
def handle_sigterm(*args):
    raise KeyboardInterrupt()

signal.signal(signal.SIGTERM, handle_sigterm)

LOGGER = utils.get_new_logger(__name__)
LOGGER = utils.configure_log(LOGGER)

LISTENING_ADDRESS = 'localhost'
LISTENING_PORT = 50051
SLEEP_TIME = 5
DEFECTIVE = False

class _MachineManager(machine_manager_pb2_grpc.MachineManagerServicer):

    def __init__(self, session_registry_manager):
        self.session_registry_manager = session_registry_manager

    def ServerShuttingDown(self, context):
        if self.session_registry_manager.shutting_down:
            context.set_details("Server is shutting down, not accepting new requests")
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            return True
        else:
            return False

    def NewSession(self, request, context):
        try:
            if self.ServerShuttingDown(context):
                return

            session_id = request.session_id
            machine_req = request.machine
            LOGGER.info("New session requested with session_id: {}".format(session_id))

            #Return the fixed initial hash
            return cartesi_machine_pb2.Hash(content=bytes.fromhex("00"))

        #No session with provided id or address issue
        except (SessionIdException, AddressException) as e:
            LOGGER.error(e)
            context.set_details("{}".format(e))
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        #Generic error catch
        except Exception as e:
            LOGGER.error("An exception occurred: {}\nTraceback: {}".format(e, traceback.format_exc()))
            context.set_details('An exception with message "{}" was raised!'.format(e))
            context.set_code(grpc.StatusCode.UNKNOWN)

    def SessionRun(self, request, context):
        try:
            if self.ServerShuttingDown(context):
                return

            #Return the fixed session run result
            summaries = [cartesi_machine_pb2.RunResponse(), cartesi_machine_pb2.RunResponse()]
            hashes = [cartesi_machine_pb2.Hash(content=bytes.fromhex("00")), cartesi_machine_pb2.Hash(content=bytes.fromhex("00"))]
            if DEFECTIVE:
                hashes = [cartesi_machine_pb2.Hash(content=bytes.fromhex("00")), cartesi_machine_pb2.Hash(content=bytes.fromhex("01"))]
            run_result = utils.make_session_run_result(summaries, hashes)
            return run_result

            session_id = request.session_id
            final_cycles = request.final_cycles
            LOGGER.info("New session run requested for session_id {} with final cycles {}".format(session_id, final_cycles))

            #Validate cycle values
            utils.validate_cycles(final_cycles)

        #No session with provided id, address issue, bad final cycles provided or problem during rollback
        except (SessionIdException, AddressException, utils.CycleException, RollbackException) as e:
            LOGGER.error(e)
            context.set_details("{}".format(e))
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        #Generic error catch
        except Exception as e:
            LOGGER.error("An exception occurred: {}\nTraceback: {}".format(e, traceback.format_exc()))
            context.set_details('An exception with message "{}" was raised!'.format(e))
            context.set_code(grpc.StatusCode.UNKNOWN)

    def SessionStep(self, request, context):
        try:
            if self.ServerShuttingDown(context):
                return

            #Return the empty session step result
            return machine_manager_pb2.SessionStepResponse()

        #No session with provided id, address issue, bad initial cycle provided or problem during rollback
        except (SessionIdException, AddressException, utils.CycleException, RollbackException) as e:
            LOGGER.error(e)
            context.set_details("{}".format(e))
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        #Generic error catch
        except Exception as e:
            LOGGER.error("An exception occurred: {}\nTraceback: {}".format(e, traceback.format_exc()))
            context.set_details('An exception with message "{}" was raised!'.format(e))
            context.set_code(grpc.StatusCode.UNKNOWN)

class _MachineDiscovery(machine_discovery_pb2_grpc.MachineDiscoveryServicer):

    def __init__(self, session_registry_manager):
        self.session_registry_manager = session_registry_manager

    def CommunicateAddress (self, request, context):
        try:
            address = request.address
            session_id = request.session_id

            LOGGER.info("Received a CommunicateAddress request for session_id {} and address {}".format(session_id, address))

            self.session_registry_manager.register_address_for_session(session_id, address)

            #Returning
            return cartesi_machine_pb2.Void()

        #No session with provided id
        except SessionIdException as e:
            LOGGER.error(e)
            context.set_details("{}".format(e))
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        #Generic error catch
        except Exception as e:
            LOGGER.error("An exception occurred: {}\nTraceback: {}".format(e, traceback.format_exc()))
            context.set_details('An exception with message "{}" was raised!'.format(e))
            context.set_code(grpc.StatusCode.UNKNOWN)

def serve(args):
    listening_add = args.address
    listening_port = args.port

    global DEFECTIVE

    #Importing the defective session registry if defective flag is set
    if args.defective:
        DEFECTIVE = True
        from defective_session_registry import SessionRegistryManager
    else:
        from session_registry import SessionRegistryManager

    manager_address = '{}:{}'.format(listening_add, listening_port)
    session_registry_manager = SessionRegistryManager(manager_address)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    machine_manager_pb2_grpc.add_MachineManagerServicer_to_server(_MachineManager(session_registry_manager),
                                                      server)
    machine_discovery_pb2_grpc.add_MachineDiscoveryServicer_to_server(_MachineDiscovery(session_registry_manager),
                                                      server)

    SERVICE_NAMES = (
        machine_manager_pb2.DESCRIPTOR.services_by_name['MachineManager'].full_name,
        machine_discovery_pb2.DESCRIPTOR.services_by_name['MachineDiscovery'].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(SERVICE_NAMES, server)
    server.add_insecure_port(manager_address)
    server.start()
    LOGGER.info("Server started, listening on address {} and port {}".format(listening_add, listening_port))
    try:
        while True:
            time.sleep(SLEEP_TIME)
    except KeyboardInterrupt:
        LOGGER.info("\nIssued to shut down")

        LOGGER.debug("Acquiring session registry global lock")
        #Acquiring lock to write on session registry
        with session_registry_manager.global_lock:
            LOGGER.debug("Session registry global lock acquired")
            session_registry_manager.shutting_down = True

        #Shutdown all active sessions servers
        for session_id in session_registry_manager.registry.keys():
            LOGGER.debug("Acquiring lock for session {}".format(session_id))
            with session_registry_manager.registry[session_id].lock:
                LOGGER.debug("Lock for session {} acquired".format(session_id))
                if (session_registry_manager.registry[session_id].address):
                    utils.shutdown_cartesi_machine_server(session_id, session_registry_manager.registry[session_id].address)

        shutdown_event = server.stop(0)

        LOGGER.info("Waiting for server to stop")
        shutdown_event.wait()
        LOGGER.info("Server stopped")

if __name__ == '__main__':

    #Adding argument parser
    description = "Instantiates a mocked machine manager server"

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        '--address', '-a',
        dest='address',
        default=LISTENING_ADDRESS,
        help='Address to listen (default: {})'.format(LISTENING_ADDRESS)
    )
    parser.add_argument(
        '--port', '-p',
        dest='port',
        default=LISTENING_PORT,
        help='Port to listen (default: {})'.format(LISTENING_PORT)
    )
    parser.add_argument(
        '--defective', '-d',
        dest='defective',
        action='store_true',
        help='Makes server behave improperly, injecting errors silently in the issued commands\n\n' + '-'*23 + 'WARNING!' + '-'*23 + 'FOR TESTING PURPOSES ONLY!!!\n' + 54*'-'
    )

    #Getting arguments
    args = parser.parse_args()

    serve(args)
