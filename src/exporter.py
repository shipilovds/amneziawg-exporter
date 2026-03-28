#!/usr/bin/env python3

import logging
import sys
import time
import json
import re
import subprocess
import signal
import argparse
from decouple import Config, RepositoryEnv, RepositoryEmpty
from datetime import datetime, timedelta
from prometheus_client import start_http_server, CollectorRegistry, Gauge, write_to_textfile


class MyLogger:
    """
    A simple wrapper around Python's logging module to set up loggers with stdout and stderr handlers.

    Parameters:
        name (str): The name of the logger.
        level (int): The logging level (default is logging.INFO).
    """
    def __init__(self, name: str, level=logging.INFO):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        formatter = logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s')
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(logging.INFO)
        stdout_handler.setFormatter(formatter)
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.ERROR)
        stderr_handler.setFormatter(formatter)
        self.logger.addHandler(stdout_handler)
        self.logger.addHandler(stderr_handler)


class Decouwrapper():
    """
    A wrapper class providing access to configuration options.

    This class reads configuration options from a file specified by the `--envfile` argument
    or uses an empty repository if the argument is not provided.
    """
    def __init__(self):
        self.__config = {}
        self.__read_config()

    def __read_config(self):
        """
        Reads configuration options from the file specified by the `--envfile` argument.

        If the `--envfile` argument is not provided, vars will be fetched from system env.
        """
        parser = argparse.ArgumentParser(description='AWG exporter options')
        parser.add_argument('--envfile', type=str, help='Path to config.env file')
        if parser.parse_args().envfile is None:
            repository = RepositoryEmpty()
        else:
            repository = RepositoryEnv(parser.parse_args().envfile)
        self.__config = Config(repository)

    def __call__(self, *args, **kwargs):
        """
        Provides access to configuration options via the Config object.
        """
        return self.__config.get(*args, **kwargs)


class AwgShowWrapper:
    """
    A wrapper class providing utility methods for parsing output from the 'awg show' command.

    This class includes static methods for parsing time strings, converting string representations of byte sizes
    to integer byte counts, parsing text blocks into structured data, and running 'awg show' commands.

    Attributes:
        None
    """


    @staticmethod
    def parse(text_block: str) -> list:
        """
        Parse a text block containing information about AmneziaWG peers into a list of dictionaries.

        Args:
            text_block (str): The text block to parse.
        """
        lines = text_block.strip().splitlines()
        peers = []
        for line in lines[1:]:  # exclude 1st line with host data
            parts = line.split()
            current_peer = {}
            if len(parts) >= 6:
                current_peer['peer'] = parts[1]
                current_peer['latest_handshake'] = parts[5]
                current_peer['received'] = parts[6]
                current_peer['sent'] = parts[7]
                peers.append(current_peer)

        return peers

    @staticmethod
    def run_bin(command: list) -> str:
        """
        Run an 'awg show' command (or its replacement) and return the output.

        Args:
            command (list[str]): The 'awg show' command to run.

        Returns:
            str: The output of the 'awg show' command.
        """
        log = MyLogger('AwgShowWrapper').logger
        try:
            process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            return process.stdout.strip()
        except subprocess.CalledProcessError as e:
            log.error(f"Error: Subprocess failed with exit code {e.returncode} and stderr: {e.stderr.strip()}")
            return ''
        except FileNotFoundError as e:
            log.error(f"{e}")
            log.error('Can not execute awg binary because of the previous exception. Exporter will not work as expected.')
            return ''
        except Exception as e:
            log.error(f"{e}")
            return ''


class Exporter():
    """
    A Prometheus exporter for collecting Amnezia WG client connection metrics.

    This class initializes the exporter, updates metrics periodically,
    and optionally exposes them via an HTTP server or writes them to a file.

    Args:
        config (dict): A dictionary containing configuration options.

    Attributes:
        config (dict): A dictionary containing configuration options.
        awg_show_command (list): A list containing the command to run the `awg show` command.
        log (Logger): A logger object for logging messages.
        registry (CollectorRegistry): A registry for registering metrics.

    Methods:
        sigterm_handler: Handles the SIGTERM signal.
        read_clients_table: Reads the client table from a file.
        write_metrics_to_file: Writes metrics to a file.
        update_metrics: Updates metrics based on `awg show` output.
        main_loop: Starts the main loop for updating metrics periodically.
    """
    def __init__(self, config: dict) -> None:
        self.config = config
        self.awg_show_command = self.config['awg_executable'].split(' ')
        self.log = MyLogger(self.__class__.__name__).logger
        self.registry = CollectorRegistry()
        self.sent_bytes_metric = Gauge('awg_sent_bytes',
                                       'Client sent bytes',
                                       ['peer', 'client_name'],
                                       registry=self.registry)
        self.received_bytes_metric = Gauge('awg_received_bytes',
                                           'Client received bytes',
                                           ['peer', 'client_name'],
                                           registry=self.registry)
        self.latest_handshake_metric = Gauge('awg_latest_handshake_seconds',
                                             'Latest client handshake with the server',
                                             ['peer', 'client_name'],
                                             registry=self.registry)
        self.status = Gauge('awg_status',
                            'Exporter status. 1 - OK, 0 - not OK',
                            registry=self.registry)
        self.log.info('AmneziaWG exporter initialized')

    def sigterm_handler(self, sig, frame):
        """
        Handles the SIGTERM signal.

        Args:
            sig: The signal number.
            frame: The current stack frame.
        """
        self.log.info('SIGTERM received, preparing to shut down...')
        sys.exit(0)

    def sigint_handler(self, sig, frame):
        """
        Handles the SIGINT signal.

        Args:
            sig: The signal number.
            frame: The current stack frame.
        """
        self.log.info('SIGINT (Ctrl+C) received, preparing to shut down...')
        sys.exit(0)

    def read_clients_table(self, file: str) -> list:
        """
        Reads the client table from a file.

        Args:
            file (str): The path to the client table file.

        Returns:
            list: A list containing client data.
        """
        try:
            with open(file) as f:
                return json.load(f)
        except Exception as e:
            self.log.error(f"Error reading clients table file: {e}")
            return []

    def write_metrics_to_file(self, metrics_file: str):
        """
        Writes metrics to a file.

        Args:
            metrics_file (str): The path to the metrics file.
        """
        write_to_textfile(metrics_file, self.registry)
        self.log.info(f"Metrics file {metrics_file} successfully updated")

    def update_metrics(self):
        """
        Updates metrics based on `awg show` output.
        """
        try:
            awg_show_result = AwgShowWrapper.run_bin(self.awg_show_command)
            parsed_data = AwgShowWrapper.parse(awg_show_result)
            if not parsed_data:
                self.status.set(0)
                return
            if not bool(self.config['clients_table_enabled']):
                clients_table = []
            else:
                clients_table = self.read_clients_table(self.config['clients_table_file'])
            for peer in parsed_data:
                client_name = next((client['userData']['clientName'] for client in clients_table if client['clientId'] == peer['peer']), 'unidentified')
                self.sent_bytes_metric.labels(peer['peer'], client_name).set(peer.get('sent', 0))
                self.received_bytes_metric.labels(peer['peer'], client_name).set(peer.get('received', 0))
                self.latest_handshake_metric.labels(peer['peer'], client_name).set(peer.get('latest_handshake', 0))
            self.status.set(1)
        except Exception as e:
            self.log.error(f"Error updating metrics: {e}")

    def main_loop(self):
        """
        Starts the main loop for updating metrics periodically.
        """
        self.log.info('Start main loop')
        signal.signal(signal.SIGTERM, self.sigterm_handler)
        signal.signal(signal.SIGINT, self.sigint_handler)
        if self.config['ops_mode'] == 'http':
            # Start up the server to expose the metrics.
            start_http_server(int(self.config['http_port']), addr=self.config['addr'], registry=self.registry)
        if not bool(self.config['clients_table_enabled']):
            self.log.info('Clients Table option is disabled. All clients will be identified as \"unidentified\"')
        while True:
            try:
                self.update_metrics()
                if self.config['ops_mode'] in ['metricsfile', 'oneshot']:
                    write_to_textfile(self.config['metrics_file'], self.registry)
                if self.config['ops_mode'] == 'oneshot':
                    self.log.info("Exiting after successful metrics fetch...")
                    break
                time.sleep(int(self.config['scrape_interval']))
            except Exception as e:
                self.log.error(f"{str(e)}")
                time.sleep(int(self.config['scrape_interval']))


if __name__ == '__main__':
    log = MyLogger("Main").logger
    log.info('Starting AmneziaWG exporter')
    config = Decouwrapper()
    exporter_config = {
        'scrape_interval': config('AWG_EXPORTER_SCRAPE_INTERVAL', default=60),
        'http_port': config('AWG_EXPORTER_HTTP_PORT', default=9351),
        'addr': config('AWG_EXPORTER_LISTEN_ADDR', default='0.0.0.0'),
        'metrics_file': config('AWG_EXPORTER_METRICS_FILE', default='/tmp/prometheus/awg.prom'),
        'ops_mode': config('AWG_EXPORTER_OPS_MODE', default='http'),
        'clients_table_enabled': config('AWG_EXPORTER_CLIENTS_TABLE_ENABLED', default='false'),
        'clients_table_file': config('AWG_EXPORTER_CLIENTS_TABLE_FILE', default='./clientsTable1'),
        'awg_executable': config('AWG_EXPORTER_AWG_SHOW_EXEC', default='awg show')
    }
    log.info('Exporter config:')
    for key, value in exporter_config.items():
        if key == 'metrics_file' and exporter_config['ops_mode'] != 'metricsfile':
            continue
        log.info(f"--> {key}: {value}")
    exporter = Exporter(exporter_config)
    exporter.main_loop()
