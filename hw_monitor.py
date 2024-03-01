# hw_monitor
#    Monitor server hardware events and forward them to Weka
# Copyright Notice:
# Copyright 2024 WekaIO. All rights reserved.
# vince@weka.io
# Loosely based on DMTF Redfish-Event-Listener, Copyright 2017-2022 DMTF. All rights reserved.
# License: BSD 3-Clause License. For full text see link: https://github.com/DMTF/Redfish-Event-Listener/blob/main/LICENSE.md
import json
import logging
import signal
import socket
import ssl
import sys
import threading
import time
import traceback
from datetime import datetime

# from redfish import redfish_client, AuthMethod
import redfish
import redfish_utilities
import wekapyutils.wekalogging as wekalogging
import yaml
from http_parser.http import HttpStream
from http_parser.reader import SocketReader
from redfish import AuthMethod
from wekalib import WekaCluster
from wekapyutils.sthreads import threaded, default_threader

# from GetDeleteiDRACSessionsREDFISH import get_current_iDRAC_sessions, delete_session

# Set up logging
wekalogging.register_module('wekalib.wekacluster', logging.ERROR)
wekalogging.register_module('wekalib.wekaapi', logging.ERROR)
wekalogging.register_module('wekapyutils', logging.ERROR)
wekalogging.register_module('redfish', logging.ERROR)
wekalogging.register_module('urllib3', logging.ERROR)
my_logger = logging.getLogger()
wekalogging.configure_logging(my_logger, logging.DEBUG)

tool_version = '1.0.0'

config = dict()


class redfish_bmc():
    """
    Class to handle Redfish BMCs
    """
    def __init__(self, redfish_ip, username=None, password=None):
        """
        Open a Redfish session to the given destination
        """
        self.redfish_ip = redfish_ip
        self.base_url = f'https://{redfish_ip}'
        self.username = username
        self.password = password
        self.rf_bmc = redfish.redfish_client(self.base_url, username, password)
        self.rf_bmc.login(auth=AuthMethod.BASIC)
        my_logger.info(f'Logged into {self.base_url} with {username} and {password}')

    def __del__(self):
        """
        Close the Redfish session
        """
        redfish_utilities.logout(self.rf_bmc, ignore_error=True)
        # self.rf_bmc.logout()

    def get_sessions(self):
        """
        Get the current sessions
        """
        response = self.rf_bmc.get('/redfish/v1/Sessions?$expand=*($levels=1)')
        if response.status != 200:
            logging.error(
                "- FAIL, GET command failed to get iDRAC session details, status code %s returned" % response.status)
            logging.error(response)
            return False
        logging.info("\n- Current running session(s) detected for iDRAC %s -\n" % self.base_url)
        id_list = list()
        for i in response.dict['Members']:
            if i['SessionType'] not in ["ManagerConsole", "WebUI"]:
                id_list.append(i['Id'])

        my_logger.info(f"Sessions: {id_list}")
        return id_list

    def get_servicetag(self):
        """
        Get the service tag
        """
        response = self.rf_bmc.get('/redfish/v1/')
        if response.status != 200:
            logging.error(
                "- FAIL, GET command failed to get iDRAC service tag, status code %s returned" % response.status)
            logging.error(response)
            return ''
        logging.info("\n- Service tag detected for iDRAC %s -\n" % self.base_url)
        if 'Oem' in response.dict and 'Dell' in response.dict['Oem'] and 'ServiceTag' in response.dict['Oem']['Dell']:
            return response.dict['Oem']['Dell']['ServiceTag']
        else:
            logging.error("Service tag not found in response.")
            return ''

    def close_sessions(self):
        """
        Close any open sessions
        """
        sessions = self.get_sessions()
        for session_id in sessions:
            my_logger.info(f'Deleting session {session_id} on {self.base_url}.')
            response = self.rf_bmc.delete(f'/redfish/v1/Sessions/{session_id}')
            if response.status != 200:
                logging.error(
                    "- FAIL, GET command failed to delete redfish session, status code %s returned" % response.status)
                logging.error(response)
                return False
            logging.info("\n- Current running session(s) detected for redfish %s -\n" % self.base_url)

    def get_subscriptions(self):
        """
        Get the current subscriptions
        """
        subscriptions = redfish_utilities.event_service.get_event_subscriptions(self.rf_bmc)

        return [subscription['@odata.id'] for subscription in subscriptions]

    def create_subscription(self, destination, client_context=None, event_types=None, format=None, expand=None,
                            resource_types=None, registries=None):
        """
        Create a new event subscription with Redfish
        """
        return redfish_utilities.create_event_subscription(self.rf_bmc,
                                                           destination,
                                                           client_context=client_context,
                                                           event_types=event_types,
                                                           format=format,
                                                           expand=expand,
                                                           resource_types=resource_types,
                                                           registries=registries)

    def cancel_subscriptions(self):
        """
        Cancel any open subscriptions
        """
        subscriptions = self.get_subscriptions()
        for subscription in subscriptions:
            try:
                my_logger.info(f'Deleting subscription {subscription} on {self.base_url}.')
                redfish_utilities.delete_event_subscription(self.rf_bmc, subscription.split('/')[-1])
            except Exception as exc:
                my_logger.error(f'Unable to delete subscription {subscription} on {self.base_url}.')
                my_logger.error(exc)

    def wait_for_job(self, job_id, jobname="None", timeout=30):
        """
        Wait for a job to complete on a Dell iDRAC (may not work with other brands)
        :param job_id:
        :type job_id:
        :param jobname:
        :type jobname:
        :param timeout:
        :type timeout:
        :return:
        :rtype:
        """
        logging.info(f"waiting for {job_id} job to complete on {self.redfish_ip}")
        # wait for the job to complete
        start_time = datetime.now()
        while (datetime.now() - start_time).seconds < timeout:
            response = self.rf_bmc.get(job_id)
            if response.status == 202:
                logging.debug(f"{jobname} job still running on {self.redfish_ip}...")
                time.sleep(5)
                continue
            elif response.status == 200:
                logging.debug(
                    f"{jobname} job completed on {self.redfish_ip} in {datetime.now() - start_time} seconds")
                time.sleep(1)
                return response
            else:
                logging.error(
                    f"GET command failed waiting for {job_id} to get job details, status code {response.status} returned on {self.redfish_ip}")
                logging.error(response)
                return None
        # timed out
        logging.error(f"{jobname} job failed to complete in {datetime.now() - start_time} seconds on {self.redfish_ip}")
        return None

    def delete_job(self, job_id):
        """
        Delete a job on a Dell iDRAC (may not work with other brands)
        :param job_id:
        :type job_id:
        :return:
        :rtype:
        """
        url = f'/redfish/v1/Dell/Managers/iDRAC.Embedded.1/DellJobService/Actions/DellJobService.DeleteJobQueue'
        payload = {"JobID": job_id.split('/')[-1]}
        response = self.rf_bmc.post(url, body=payload)
        if response.status != 200:
            logging.error(
                f"POST command failed to delete job {job_id} on {self.base_url}, status code {response.status} returned")
            logging.error(response)
            return False
        logging.debug(f'Job ID "{job_id}" deleted on {self.base_url}')
        time.sleep(5)

    def generate_import_buffer(self, target_fqdd):
        """
        Generate an import buffer for a Dell iDRAC (may not work with other brands)
        :param target_fqdd:
        :type target_fqdd:
        :return:
        :rtype:
        """
        logging.info(
            f'applying quick alert settings for {self.redfish_ip}, this may take up to 30 seconds to complete')

        url = f'/redfish/v1/Managers/iDRAC.Embedded.1/Actions/Oem/EID_674_Manager.ExportSystemConfiguration'
        payload = {"ExportFormat": "XML", "ShareParameters": {"Target": target_fqdd}}
        jobname = "ExportSystemConfiguration"
        response = self.rf_bmc.post(url, body=payload)

        # check if the job was created?   This next line could be a problem if the response is not a 202
        if response.status != 202:
            logging.error(
                f"POST command failed to create job for {jobname} method on {self.redfish_ip}, status code {response.status} returned")
            logging.error(response)
            return None
        job_id = response.task_location
        logging.debug(f'Job ID "{job_id}" created for {jobname} method on {self.redfish_ip}')

        if job_id is None:
            logging.error(f"- FAIL, POST command failed to create job for {jobname} method on {self.redfish_ip}")
            logging.error(response)
            return None

        # wait for the job to complete
        start_time = datetime.now()
        response = self.wait_for_job(job_id, jobname, 90)
        if response == None:
            logging.error(f"{jobname} job failed to complete on {self.redfish_ip}")
            self.delete_job(job_id)
            return False

        # if we get here, the job is done
        dict_output = response.__dict__
        if '<SystemConfiguration Model' in str(dict_output):
            logging.debug(f'{jobname} job completed successfully on {self.redfish_ip}')
            orig_buffer = import_buffer = dict_output['_read'].decode('utf-8')
            #
            # Severity levels - informational = 3, warning = 2, critical = 1
            min_severity = 2  # set the minimum severity to warning
            severity_set = set((list(range(min_severity, 0, -1))))

            # turn on redfish eventing for the severities we want
            for sev in severity_set:
                import_buffer = import_buffer.replace(
                    f'{sev}#Alert#RedfishEventing">Disabled', f'{sev}#Alert#RedfishEventing">Enabled')

            max_sev_set = {1, 2, 3}
            sevs_to_remove = max_sev_set - severity_set

            # turn off the ones we don't want
            for sev in sevs_to_remove:
                import_buffer = import_buffer.replace(
                    f'{sev}#Alert#RedfishEventing">Enabled', f'{sev}#Alert#RedfishEventing">Disabled')

            if orig_buffer == import_buffer:
                logging.debug(f'No changes made to the import buffer for {self.redfish_ip}')
                self.delete_job(job_id)
                return None
            # we're done with the job, delete it
        else:
            logging.error(f"{jobname} job didn't return data on {self.redfish_ip}")
            self.delete_job(job_id)
            return None
            # break

        total_time = (datetime.now() - start_time)
        logging.debug(f'Quick alert settings created for {self.redfish_ip} in {total_time.seconds} seconds')
        self.delete_job(job_id)
        return import_buffer

    def scp_import_buffer(self, import_buffer):
        """
        send the import buffer to the iDRAC
        :param import_buffer:
        :type import_buffer:
        :return:
        :rtype:
        """
        url = f'/redfish/v1/Managers/iDRAC.Embedded.1/Actions/Oem/EID_674_Manager.ImportSystemConfiguration'
        payload = {"ImportBuffer": import_buffer, "ShareParameters": {"Target": "ALL"}}
        jobname = "ImportSystemConfiguration"
        response = self.rf_bmc.post(url, body=payload)
        if response.status != 202:
            logging.error(
                f"POST command failed to create job for {jobname} method on {self.redfish_ip}, status code {response.status} returned")
            logging.error(response)
            return False
        job_id = response.task_location
        logging.debug(f'Job ID "{job_id}" created for {jobname} method on {self.redfish_ip}')
        response = self.wait_for_job(job_id, jobname, 90)
        if response == None:
            logging.error(f"{jobname} job failed to complete on {self.redfish_ip}")
            self.delete_job(job_id)
            return False

        # if we get here, the job is done
        try:
            job_message = response.dict['Messages'][0]['Message']
        except:
            logging.error(
                f"GET command failed to get job details, status code {response.status} returned on {self.redfish_ip}")
            logging.error(response.dict)
            self.delete_job(job_id)
            return False

        logging.info(f'{jobname} job completed on {self.redfish_ip} with message: {job_message}')

        try:
            job_state = response.dict['Oem']['Dell']['JobState']
        except:
            logging.error(
                f"GET command failed to get job details, status code {response.status} returned on {self.redfish_ip}")
            logging.error(response)
            self.delete_job(job_id)
            return False

        if job_state == 'Failed':
            logging.error(f'{jobname} job failed on {self.redfish_ip}')
            self.delete_job(job_id)
            return False

            # break
        self.delete_job(job_id)
        return True

    @threaded
    def set_alert_config(self):
        """
        Set the alert configuration
        """
        url = f'/redfish/v1/Managers/iDRAC.Embedded.1/Attributes'
        payload = {"Attributes": {"IPMILan.1.AlertEnable": "Enabled"}}
        try:
            response = self.rf_bmc.patch(url, body=payload)
        except Exception as exc:
            my_logger.error(f'Unable to set alert configuration on {self.base_url}.')
            my_logger.error(exc)
            return False
        if response.status != 200:
            logging.error(
                "- FAIL, PATCH command failed to set iDRAC alert configuration, status code %s returned" % response.status)
            logging.error(response)
            return False
        target_fqdd_list = ["EventFilters.SystemHealth.1", "EventFilters.Storage.1",
                            "EventFilters.Configuration.1", "EventFilters.Audit.1",
                            "EventFilters.Updates.1"]
        for target_fqdd in target_fqdd_list:
            import_buffer = self.generate_import_buffer(target_fqdd)
            if import_buffer is not None:
                self.scp_import_buffer(import_buffer)

        return True


def send_event_to_weka(event):
    """
    Send an event to Weka as a Custom Event
    :param event:
    :type event:
    :return:
    :rtype:
    """
    my_logger.info(f'Sending event to Weka: {event}')
    try:
        cluster = WekaCluster(config['wekaclusterip'], config['wekaauthtoken'],
                              force_https=config['force_https'],
                              verify_cert=config['verify_cert'])
        cluster.call_api('events_trigger_custom', {'message': event[:128]})
    except Exception as err:
        my_logger.error(err)
        my_logger.error(traceback.print_exc())
        return False
    return True


### Function to read data in json format using HTTP Stream reader, parse Headers and Body data, Response status OK to service and Update the output into file
def process_data(newsocketconn, fromaddr, credentials_dict):
    """
    Process the data from the socket connection - this catches the events from the BMCs and sends them to Weka
    :param newsocketconn:
    :type newsocketconn:
    :param fromaddr:
    :type fromaddr:
    :return:
    :rtype:
    """
    if useSSL:
        connstreamout = context.wrap_socket(newsocketconn, server_side=True)
    else:
        connstreamout = newsocketconn
    ### Output File Name
    outputfile = "Events_" + str(fromaddr[0]) + ".txt"
    logfile = "TimeStamp.log"
    global event_count, data_buffer
    outdata = headers = HostDetails = ""
    try:
        try:
            ### Read the json response using Socket Reader and split header and body
            r = SocketReader(connstreamout)
            p = HttpStream(r)
            headers = p.headers()
            my_logger.debug("headers: %s", headers)

            if p.method() == 'POST':
                bodydata = p.body_file().read().decode("utf-8")
                my_logger.debug("\n")
                my_logger.debug("bodydata: %s", bodydata)
                data_buffer.append(bodydata)
                for eachHeader in headers.items():
                    if eachHeader[0] == 'Host' or eachHeader[0] == 'host':
                        HostDetails = eachHeader[1]

                ### Read the json response and print the output
                my_logger.info("\n")
                my_logger.info("Event Received from %s", fromaddr)
                # my_logger.info("Server IP Address is %s", fromaddr[0])
                my_logger.info("Sender PORT number is %s", fromaddr[1])
                # my_logger.info("Listener IP is %s", HostDetails)
                service_tag = ''
                sender = ''
                if len(fromaddr) != 2:
                    my_logger.info("Invalid Host Details")
                else:
                    sender = fromaddr[0]
                if sender in credentials_dict.keys():
                    user, passwd = credentials_dict[sender]
                    bmc = redfish_bmc(sender, user, passwd)
                    service_tag = bmc.get_servicetag()
                else:
                    my_logger.info("Unable to get Service Tag for the Host %s", sender)
                outdata = json.loads(bodydata)
                serverhostname = ""
                if 'Oem' in outdata:
                    oem_dict = outdata['Oem']
                    oem = list(oem_dict.keys())[0]
                    my_logger.info("Oem is %s", oem)
                    if 'Dell' in oem_dict and 'ServerHostname' in oem_dict['Dell']:
                        serverhostname = oem_dict['Dell']['ServerHostname']
                        my_logger.info("Server Host Name is %s", serverhostname)
                    my_logger.debug("\n")
                if 'Events' in outdata:
                    for event in outdata['Events']:
                        # my_logger.info("EventType is %s", event['EventType']) # in event.items() below
                        # my_logger.info("MessageId is %s", event['MessageId'])
                        for item, value in event.items():
                            my_logger.info("%s is %s", item, value)
                        with open(logfile, 'a') as f:
                            if 'EventTimestamp' in outdata:
                                receTime = datetime.now()
                                sentTime = datetime.strptime(event['EventTimestamp'], "%Y-%m-%d %H:%M:%S.%f")
                                f.write("%s    %s    %sms\n" % (
                                    sentTime.strftime("%Y-%m-%d %H:%M:%S.%f"), receTime,
                                    (receTime - sentTime).microseconds / 1000))
                            else:
                                f.write('No available timestamp.\n')

                        msg_text = f'{service_tag}:{serverhostname}:{sender}:{event["EventTimestamp"]}:{event["Severity"]}:{event["Message"]}'

                        my_logger.info(msg_text)
                        if send_event_to_weka(msg_text):
                            my_logger.info("Event sent to Weka successfully.")
                        else:
                            my_logger.error("Event failed to send to Weka.")
                        with open(outputfile, "a") as fd:
                            fd.write(f'{msg_text}')

                ### Check the context and send the status OK if context matches
                if config['contextdetail'] is not None and outdata.get('Context', None) != config['contextdetail']:
                    my_logger.info("Context ({}) does not match with the server ({})."
                                   .format(outdata.get('Context', None), config['contextdetail']))
                res = "HTTP/1.1 204 No Content\r\n" \
                      "Connection: close\r\n" \
                      "\r\n"
                connstreamout.send(res.encode())

                try:
                    if event_count.get(str(fromaddr[0])):
                        event_count[str(fromaddr[0])] = event_count[str(fromaddr[0])] + 1
                    else:
                        event_count[str(fromaddr[0])] = 1

                    my_logger.info("Event Counter for Host %s = %s" % (str(fromaddr[0]), event_count[fromaddr[0]]))
                    # my_logger.info("\n")
                    # fd = open(outputfile, "a")
                    # message = f'Received: {datetime.now()} Count: {event_count[str(fromaddr[0])]} Host IP: {str(fromaddr)} Event Details: {json.dumps(outdata)}'
                    # fd.write(message)
                    # my_logger.info(message)
                    # fd.close()
                except Exception as err:
                    my_logger.info(err)
                    my_logger.info(traceback.print_exc())

            if p.method() == 'GET':
                # for x in data_buffer:
                #     my_logger.info(x)
                res = "HTTP/1.1 200 OK\n" \
                      "Content-Type: application/json\n" \
                      "\n" + json.dumps(data_buffer)
                connstreamout.send(res.encode())
                data_buffer.clear()

        except Exception as err:
            outdata = connstreamout.read()
            my_logger.info(f"Data needs to read in normal Text format. {outdata}")
            my_logger.info(outdata)

    finally:
        connstreamout.shutdown(socket.SHUT_RDWR)
        connstreamout.close()


import argparse

if __name__ == '__main__':
    """
    Main program
    """

    # Print the tool banner
    # logging.info('Redfish Event Listener v{}'.format(tool_version))

    argget = argparse.ArgumentParser(
        description=f'hw_monitor (v{tool_version}) deploys an HTTP(S) server to receive and forward events server BMCs')

    # config
    argget.add_argument('-c', '--config', type=str, default='./hw_monitor.yml',
                        help='Location of the configuration file; default: "./hw_moniitor.yml"')
    argget.add_argument('-v', '--verbose', action='count', default=0, help='Verbose output')
    argget.add_argument("--version", dest="version", default=False, action="store_true", help="Display version number")
    args = argget.parse_args()

    if args.version:
        print(f'{sys.argv[0]} version {tool_version}')
        sys.exit(0)

    # Initiate Configuration File

    parsed_config = yaml.load(open(args.config, 'r'), Loader=yaml.FullLoader)

    # Host Info
    config['systeminformation'] = parsed_config['SystemInformation']
    if 'SystemInformation' in parsed_config:
        config['listenerip'] = parsed_config['SystemInformation'].get('ListenerIP')
        config['listenerport'] = parsed_config['SystemInformation'].get('ListenerPort')
        config['usessl'] = parsed_config['SystemInformation'].get('UseSSL')

    # Cert Info
    if config['usessl'] and 'CertificateDetails' in parsed_config:
        config['certfile'] = parsed_config['CertificateDetails'].get('certfile')
        config['keyfile'] = parsed_config['CertificateDetails'].get('keyfile')

    # Subscription Details
    if "SubscriptionDetails" in parsed_config:
        config['destination'] = parsed_config["SubscriptionDetails"].get('Destination')
        config['contextdetail'] = parsed_config["SubscriptionDetails"].get('Context')
        config['eventtypes'] = parsed_config["SubscriptionDetails"].get('EventTypes')
        config['format'] = parsed_config["SubscriptionDetails"].get('Format')
        config['expand'] = parsed_config["SubscriptionDetails"].get('Expand')
        config['resourcetypes'] = parsed_config["SubscriptionDetails"].get('ResourceTypes')
        config['registries'] = parsed_config["SubscriptionDetails"].get('Registries')

    # Subscription Targets
    if 'ServerInformation' in parsed_config:
        config['bmcIPs'] = parsed_config['ServerInformation'].get('BmcIPs')
        config['usernames'] = parsed_config['ServerInformation'].get('UserNames')
        config['passwords'] = parsed_config['ServerInformation'].get('Passwords')
        # config['logintype'] = ['Session' for x in config['bmcURLs']]
        # if parsed_config.has_option('ServerInformation', 'LoginType'):
        #    config['logintype'] = parse_list(parsed_config.get('ServerInformation', 'LoginType'))
        #    config['logintype'] += ['Session'] * (len(config['bmcURLs']) - len(config['logintype']))

    # Other Info
    config['verbose'] = args.verbose
    if config['verbose']:
        print(json.dumps(config, indent=4))

    # WEKA info
    config['wekaclusterip'] = parsed_config['cluster'].get('hosts')
    config['wekaauthtoken'] = parsed_config['cluster'].get('auth_token_file')
    config['force_https'] = parsed_config['cluster'].get('force_https')
    config['verify_cert'] = parsed_config['cluster'].get('verify_cert')

    bmc_dict = dict()
    credentials_dict = dict()
    target_contexts = []
    if not (len(config['bmcIPs']) == len(config['usernames']) == len(config['passwords'])):
        my_logger.error("Number of BmcIPs does not match UserNames, Passwords, or LoginTypes")
        sys.exit(1)
    elif len(config['bmcIPs']) == 0:
        my_logger.info("No subscriptions are specified. Continuing with Listener.")
    else:
        # build a dict of the BMCs so we can log into them (a more convenient format)
        for dest, user, passwd in zip(config['bmcIPs'], config['usernames'], config['passwords']):
            credentials_dict[dest] = (user, passwd)

    # Clean up and set up the BMCs
    logging.debug('')
    logging.info(
        '################################################# Logging into BMCs #################################################')
    for dest, credentials in credentials_dict.items():
        user, passwd = credentials
        # log into the RedFish BMC
        my_logger.info(f'Logging into {dest} with {user} and {passwd}.')
        bmc_dict[dest] = redfish_bmc(dest, user, passwd)
        if bmc_dict[dest] is None:
            my_logger.error(f'Unable to log into {dest} with {user} and {passwd}.')
            continue

    logging.debug('')
    logging.info(
        '################################################# Canceling subscriptions and sessions #################################################')
    for dest, bmc in bmc_dict.items():
        # log into the RedFish BMC
        if bmc is None:
            my_logger.error(f'Not logged in to {dest}, skipping canceling subscriptions and sessions.')
            continue

        my_logger.info(f'Canceling subscriptions and sessions on {dest}.')
        bmc.cancel_subscriptions()
        bmc.close_sessions()

    logging.debug('')
    logging.info(
        '################################################# Setting Alert Config #################################################')
    for dest, bmc in bmc_dict.items():
        # we just killed our own session (can't tell which it is) so we need to re-login
        if bmc is None:
            my_logger.error(f'Not logged in to {dest}, skipping setting alert config.')
            continue

        # need to multi-thread this
        bmc.set_alert_config()

    default_threader.run()

    # Create the subscriptions on the Redfish services provided
    logging.debug('')
    logging.info(
        '################################################# Create Subscriptions #################################################')
    for dest, bmc in bmc_dict.items():
        try:
            # Log in to the service
            my_logger.info("ServerIP:: {}".format(dest))
            #my_logger.info("UserName:: {}".format(user))

            if bmc is None:
                my_logger.error(f'Not logged in to {dest}, skipping creating subscription.')
                continue

            # Create the subscription
            response = bmc.create_subscription(config['destination'],
                                               client_context=config['contextdetail'],
                                               event_types=config['eventtypes'],
                                               format=config['format'],
                                               expand=config['expand'],
                                               resource_types=config['resourcetypes'],
                                               registries=config['registries'])

            # Save the subscription info for deleting later
            my_location = response.getheader('Location')
            my_logger.info(f"Subscription is successful for {dest}, {my_location}")
            unsub_id = None
            try:
                # Response bodies are expected to have the event destination
                unsub_id = response.dict['Id']
            except:
                # Fallback to determining the Id from the Location header
                if my_location is not None:
                    unsub_id = my_location.strip('/').split('/')[-1]
            if unsub_id is None:
                my_logger.error(f'{dest} did not provide a location for the subscription; cannot unsubscribe')
            else:
                target_contexts.append((dest, bmc.rf_bmc, unsub_id))
        except Exception as e:
            my_logger.info('Unable to subscribe for events with {}'.format(dest))
            my_logger.info(traceback.print_exc())

        del bmc

        my_logger.info("Continuing with Listener.")

    # Accept the TCP connection using certificate validation using Socket wrapper
    useSSL = config['usessl']
    if useSSL:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile=config['certfile'], keyfile=config['keyfile'])

    # Bind socket connection and listen on the specified port
    my_host = (config['listenerip'], config['listenerport'])
    my_logger.info(
        'Listening on {}:{} via {}'.format(config['listenerip'], config['listenerport'], 'HTTPS' if useSSL else 'HTTP'))
    event_count = {}
    data_buffer = []

    my_logger.info('Press Ctrl-C to close program')

    # Check if the listener is IPv4 or IPv6; defaults to IPv4 if the lookup fails
    try:
        family = socket.getaddrinfo(config['listenerip'], config['listenerport'])[0][0]
    except:
        family = socket.AF_INET
    socket_server = socket.create_server(my_host, family=family)
    socket_server.listen(5)
    socket_server.settimeout(3)


    def handler_end(sig, frame):
        my_logger.error('\nPress Ctrl-C again to skip unsubscribing and logging out.\n')
        signal.signal(signal.SIGINT, lambda x, y: sys.exit(1))


    def handler(sig, frame):
        my_logger.info('Closing all our subscriptions')
        signal.signal(signal.SIGINT, handler_end)
        socket_server.close()

        for name, ctx, unsub_id in target_contexts:
            my_logger.info('\nClosing {}'.format(name))
            my_logger.info(f'Unsubscribing from ctx={ctx}, unsub_id={unsub_id}')
            try:
                redfish_utilities.delete_event_subscription(ctx, unsub_id)
                ctx.logout()
            except redfish_utilities.event_service.RedfishEventServiceNotFoundError as exc:
                my_logger.error(f"{exc}; with {ctx.get_base_url()} and {unsub_id}")
            except:
                my_logger.info('Unable to unsubscribe for events with {}'.format(ctx.get_base_url()))
                my_logger.info(traceback.print_exc())

        sys.exit(0)


    signal.signal(signal.SIGINT, handler)

    my_logger.info('#################################################')
    my_logger.info('Waiting for connections...')
    my_logger.info('#################################################')
    #count = 0
    while True:
        newsocketconn = None
        try:
            ### Socket Binding
            newsocketconn, fromaddr = socket_server.accept()
            #count += 1
            try:
                ### Multiple Threads to handle different request from different servers
                my_logger.info('\nSocket connected::')
                threading.Thread(target=process_data, args=(newsocketconn, fromaddr, credentials_dict)).start()
            except Exception as err:
                my_logger.info(traceback.print_exc())
        except socket.timeout:
            # if count % 100 == 0:
            #    print('.', end='', flush=True)
            #    if count % 1000 == 0:
            #        print('', flush=True)
            # else:
            #    if count > 10000:
            #        count = 0
            pass
        except Exception as err:
            my_logger.info("Exception occurred in socket binding.")
            my_logger.info(traceback.print_exc())
