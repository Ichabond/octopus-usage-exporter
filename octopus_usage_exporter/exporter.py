"""Main exporter class for Octopus Energy usage data."""

from prometheus_client import MetricsHandler, Gauge
import httpx
from datetime import datetime, timedelta
from jose import jwt
import logging
import os
import threading
import time
from http.server import HTTPServer
from pathlib import Path
from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport, log as requests_logger
from gql.transport.exceptions import TransportQueryError

from .energy_meter import EnergyMeter

logger = logging.getLogger(__name__)
requests_logger.setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

class PrometheusEndpointServer(threading.Thread):
    """Thread for running the Prometheus metrics server."""
    
    def __init__(self, httpd, *args, **kwargs):
        self.httpd = httpd
        super(PrometheusEndpointServer, self).__init__(*args, **kwargs)

    def run(self):
        self.httpd.serve_forever()


class OctopusEnergyExporter:
    """Main exporter class for Octopus Energy usage data."""
    
    def __init__(self, api_key, account_number, prom_port, interval=1800, gas=False, electric=False, 
                 ng_metrics=False, tariff_rates=False, tariff_remaining=False):
        self.api_key = api_key
        self.jwt = None
        self.interval = self.interval_rate_check(interval)
        self.gas = gas
        self.electric = electric
        self.ng_metrics = ng_metrics
        self.tariff_rates = tariff_rates
        self.tariff_remaining = tariff_remaining
        self.headers = {}
        self._jwt_token = ""
        
        self.gauges = {}
        self.meters = []
        self.sysconfig = {}
        
        self.prom_port = prom_port
        self.account_number = account_number
        
        # Initialize HTTP client and GraphQL client
        self._get_jwks()
        self.setup_clients()
        self.get_jwt()
        self.setup_clients()

    def _get_jwks(self):
        # Get JWKS for JWT validation
        response = httpx.get(url="https://auth.octopus.energy/.well-known/jwks.json")
        self.key = response.json()

    def validate_jwt(self):
        user_info =jwt.decode(self._jwt_token, key=self.key, algorithms=["RS256"])
        if (datetime.fromtimestamp(user_info["exp"]) > datetime.now() + timedelta(minutes=2)):
            logger.info("JWT valid until {}".format(datetime.fromtimestamp(user_info["exp"])))
            return True
        else:
            logger.info(f"JWT expired, requires refresh: {datetime.fromtimestamp(user_info['exp'])}")
            return False
        
    def get_jwt(self):
        query = gql("""
            mutation ObtainKrakenToken($apiKey: String!) {
                obtainKrakenToken(input: { APIKey: $apiKey}) {
                    token
                }
            }
        """)
        result = self.oe_client.execute(query, variable_values={"apiKey": self.api_key})
        self._jwt_token = result['obtainKrakenToken']['token']
        logger.debug(f"JWT token: {self._jwt_token}")
        if self.validate_jwt():
            self.headers['Authorization'] = f"JWT {self._jwt_token}"
        else:
            logger.error("Failed to validate JWT, cannot continue")
            raise Exception("Failed to validate JWT, cannot continue")

    def setup_clients(self):
        """Initialize HTTP and GraphQL clients."""
        # Setup GraphQL client
        transport = RequestsHTTPTransport(
            url="https://api.octopus.energy/v1/graphql/#", 
            headers=self.headers, 
            verify=True, 
            retries=3
        )
        self.oe_client = Client(transport=transport, fetch_schema_from_transport=False)
    
    def start_prometheus_server(self):
        """Start the Prometheus metrics server."""
        try:
            httpd = HTTPServer(("", self.prom_port), MetricsHandler)
            thread = PrometheusEndpointServer(httpd)
            thread.daemon = True
            thread.start()
            logger.info(f"Prometheus server started on port {self.prom_port}")
        except Exception as e:
            logger.error(f"Failed to start Prometheus server: {e}")
            raise

    def get_device_id(self):
        """Get device IDs for gas and electric meters."""
        gas_query = gql("""
            query Account($accountNumber: String!) {
                account(accountNumber: $accountNumber) {
                    id
                    gasAgreements {
                        id
                        ... on GasAgreementType {
                            id
                            tariff {
                                displayName
                            }
                        }
                        meterPoint {
                            id
                            meters {
                                id
                                smartGasMeter {
                                    id
                                    deviceId
                                }
                            }
                        }
                    }
                }
            }
        """)

        elec_query = gql("""
            query Account($accountNumber: String!) {
                account(accountNumber: $accountNumber) {
                    id
                    electricityAgreements {
                        id
                        ... on ElectricityAgreementType {
                            id
                            tariff {
                                ... on StandardTariff {
                                    displayName
                                }
                                ... on DayNightTariff {
                                    displayName
                                }
                                ... on ThreeRateTariff {
                                    displayName
                                }
                                ... on HalfHourlyTariff {
                                    displayName
                                }
                                ... on PrepayTariff {
                                    displayName
                                }
                            }
                        }
                        meterPoint {
                            id
                            meters {
                                smartImportElectricityMeter {
                                    id
                                    deviceId
                                }
                            }
                        }
                    }
                }
            }
        """)
        if self.electric:
            electric_query = self.oe_client.execute(elec_query, variable_values={"accountNumber": self.account_number})
            usable_smart_meters = [m for m in electric_query["account"]["electricityAgreements"][0]["meterPoint"]["meters"]
                                   if m['smartImportElectricityMeter'] is not None]
            selected_smart_meter_device_id = usable_smart_meters[0]["smartImportElectricityMeter"]["deviceId"]
            self.meters.append(EnergyMeter("electric_meter", selected_smart_meter_device_id, "electric", self.interval, datetime.now()-timedelta(seconds=self.interval), ["consumption", "demand"], electric_query["account"]["electricityAgreements"][0]["id"] ))
            logger.info("Electricity Meter has been found - {}".format(selected_smart_meter_device_id))
            logger.info("Electricity Tariff information: {}".format(electric_query["account"]["electricityAgreements"][0]["tariff"]["displayName"]))
        if self.gas:
            gas_query = self.oe_client.execute(gas_query, variable_values={"accountNumber": self.account_number})
            usable_smart_meters = [m for m in gas_query["account"]["gasAgreements"][0]["meterPoint"]["meters"]
                                   if m['smartGasMeter'] is not None]
            selected_smart_meter_device_id = usable_smart_meters[0]["smartGasMeter"]["deviceId"]
            self.meters.append(EnergyMeter("gas_meter", selected_smart_meter_device_id, "gas", self.interval, datetime.now()-timedelta(seconds=self.interval), ["consumption"], gas_query["account"]["gasAgreements"][0]["id"]))
            logger.info("Gas Meter has been found - {}".format(selected_smart_meter_device_id))
            logger.info("Gas Tariff information: {}".format(gas_query["account"]["gasAgreements"][0]["tariff"]["displayName"]))

    def get_energy_reading(self, meter_id, reading_types, agreement_id, energy_type):
        """Get energy readings for a specific meter."""
        output_readings = {}
        # Dynamically build the query based on which agreement IDs are provided
        query_blocks = []
        query_blocks.append("""
                smartMeterTelemetry(deviceId: $deviceId) {
                    readAt
                    consumption
                    demand
                    consumptionDelta
                    costDelta
                }
        """)
        if energy_type == "electric":
            query_blocks.append("""
                electricityAgreement(id: $electricityAgreementId) {
                    isRevoked
                    validTo
                    ... on ElectricityAgreementType {
                        id
                        validTo
                        agreedFrom
                        tariff {
                            ... on StandardTariff {
                                id
                                displayName
                                standingCharge
                                isExport
                                unitRate
                            }
                            ... on DayNightTariff {
                                id
                                displayName
                                fullName
                                standingCharge
                                isExport
                                dayRate
                                nightRate
                            }
                            ... on ThreeRateTariff {
                                id
                                displayName
                                standingCharge
                                isExport
                                dayRate
                                nightRate
                                offPeakRate
                            }
                            ... on HalfHourlyTariff {
                                id
                                displayName
                                standingCharge
                                isExport
                                unitRates {
                                    validFrom
                                    validTo
                                    value
                                }
                            }
                            ... on PrepayTariff {
                                id
                                displayName
                                description
                                standingCharge
                                isExport
                                unitRate
                            }
                        }
                    }
                }
            """)
        elif energy_type == "gas":
            query_blocks.append("""
                gasAgreement(id: $gasAgreementId) {
                    validTo
                    isRevoked
                    id
                    validFrom
                    ... on GasAgreementType {
                        id
                        isRevoked
                        tariff {
                            id
                            displayName
                            fullName
                            standingCharge
                            isExport
                            unitRate
                        }
                    }
                }
            """)

        query_blocks_joined = "\n".join(query_blocks)
        query_str = f"""
        query TariffsandMeterReadings($deviceId: String!{', $electricityAgreementId: ID!' if energy_type == "electric" else ''}{', $gasAgreementId: ID!' if energy_type == "gas" else ''}) {{
                {query_blocks_joined}
            }}
        """

        query = gql(query_str)
        variables = {"deviceId": meter_id}
        if energy_type == "electric":
            variables["electricityAgreementId"] = agreement_id
        elif energy_type == "gas":
            variables["gasAgreementId"] = agreement_id

        try:
            reading_query_ex = self.oe_client.execute(query, variable_values=variables)
            reading_query_returned = reading_query_ex["smartMeterTelemetry"][0]
            if energy_type == "electric" and (self.tariff_rates or self.tariff_remaining):
                if reading_query_ex["electricityAgreement"]["isRevoked"]:
                    logger.warning(f"Electricity agreement {agreement_id} is revoked, no tariff information will be returned.")
                    return {}
                if reading_query_ex["electricityAgreement"]["validTo"]:
                    valid_to = datetime.fromisoformat(reading_query_ex["electricityAgreement"]["validTo"])
                    if valid_to < datetime.now(valid_to.tzinfo):
                        logger.warning(f"Electricity agreement {agreement_id} is no longer valid, no tariff information will be returned")
                        return {}
                for key, value in self.electricity_tariff_parser(reading_query_ex["electricityAgreement"]).items():
                    output_readings[key] = value
            elif energy_type == "gas" and (self.tariff_rates or self.tariff_remaining):
                if reading_query_ex["gasAgreement"]["isRevoked"]:
                    logger.warning(f"Gas agreement {agreement_id} is revoked, no tariff information will be returned.")
                    return {}
                if reading_query_ex["gasAgreement"]["validTo"]:
                    valid_to = datetime.fromisoformat(reading_query_ex["gasAgreement"]["validTo"])
                    if valid_to < datetime.now(valid_to.tzinfo):
                        logger.warning(f"Gas agreement {agreement_id} is no longer valid, no tariff information will be returned")
                        return {}
                if self.tariff_rates:
                    output_readings["tariff_unit_rate"] = reading_query_ex["gasAgreement"]["tariff"]["unitRate"] if self.tariff_rates else None
                    output_readings["tariff_standing_charge"] = reading_query_ex["gasAgreement"]["tariff"]["standingCharge"]
                if self.tariff_remaining:
                    output_readings["tariff_days_remaining"] = (datetime.fromisoformat(reading_query_ex["gasAgreement"]["validTo"]) - datetime.now(datetime.fromisoformat(reading_query_ex["gasAgreement"]["validTo"]).tzinfo)).days if reading_query_ex["gasAgreement"]["validTo"] else None
            for wanted_type in reading_types:
                if reading_query_returned[wanted_type] is None:
                    output_readings[wanted_type] = 0
                else:
                    output_readings[wanted_type] = reading_query_returned[wanted_type]
                logger.info(f"Meter: {meter_id} - Type: {wanted_type} - Reading: {reading_query_returned[wanted_type]}")
        except TransportQueryError as e:
            logger.warning("Possible rate limit hit, increase call interval")
            logger.warning(e)
        except IndexError:
            if not reading_query_ex["smartMeterTelemetry"]:
                logger.error(f"Octopus API returned no data for {meter_id}")
        
        return output_readings

    def electricity_tariff_parser(self, tariff):
        """Parse electricity tariff information."""
        output_map = {}

        if tariff["tariff"]["isExport"]:
            logger.debug("This is an export tariff, no unit rates will be returned")
            return output_map

        now = datetime.now().astimezone()
        t = tariff["tariff"]
        if self.tariff_rates:
            if t.get("unitRates"):
                logger.debug("Octopus 'smart' tariff detected. Half hourly rates will be returned.")
                # Find the unit rate valid for now
                current_rate = None
                for rate in t["unitRates"]:
                    valid_from = datetime.fromisoformat(rate["validFrom"])
                    valid_to = datetime.fromisoformat(rate["validTo"])
                    if valid_from <= now and now < valid_to:
                        current_rate = rate["value"]
                        break
                output_map["tariff_unit_rate"] = current_rate
            elif t.get("dayRate") and t.get("nightRate") and t.get("offPeakRate"):
                logger.warning("Octopus 'three rate' tariff detected. Support for this tariff is not available yet.")
                return output_map
            elif t.get("dayRate") and t.get("nightRate"):
                logger.warning("Octopus 'day night' tariff detected. Support for this tariff is not available yet.")
                return output_map
            elif t.get("unitRate"):
                logger.debug("Octopus 'standard/prepay' tariff detected. Single unit rate will be returned.")
                output_map["tariff_unit_rate"] = t["unitRate"]

            output_map["tariff_standing_charge"] = t["standingCharge"]
        if self.tariff_remaining:
            valid_to = tariff.get("validTo")
            if valid_to:
                valid_to_dt = datetime.fromisoformat(valid_to)
                now = datetime.now(valid_to_dt.tzinfo)
                output_map["tariff_days_remaining"] = (valid_to_dt - now).days
            else:
                output_map["tariff_days_remaining"] = None
        
        return output_map

    def update_gauge(self, key, value):
        """Update a Prometheus gauge metric."""
        if key not in self.gauges:
            self.gauges[key] = Gauge(key, f'Octopus Energy metric: {key}')
        self.gauges[key].set(value)

    def update_gauge_ng(self, key: str, value: int, labels_dict: dict):
        """Update a Prometheus gauge with labels."""
        if key not in self.gauges:
            label_names = list(labels_dict.keys()) if labels_dict else []
            self.gauges[key] = Gauge(key, f'Octopus Energy metric: {key}', label_names)
        
        if labels_dict:
            self.gauges[key].labels(**labels_dict).set(value)
        else:
            self.gauges[key].set(value)


    def read_meters(self):
        """Main loop to read meter data."""
        while True:
            try:
                if not self.validate_jwt():
                    self.get_jwt()                
                for meter in self.meters:
                    current_time = datetime.now()
                    if current_time - meter.last_called >= timedelta(seconds=meter.polling_interval):
                        logger.info(f"Reading {meter.meter_type} meter: {meter.device_id}")
                        
                        # Get energy readings
                        readings = self.get_energy_reading(
                            meter.device_id, 
                            meter.reading_types, 
                            meter.agreement, 
                            meter.meter_type
                        )
                        
                        # Update Prometheus metrics
                        if readings:
                            labels = meter.return_labels()
                            for reading_type, value in readings.items():
                                if self.ng_metrics:
                                    metric_name = f"octopus_{meter.meter_type}_{reading_type}"
                                    self.update_gauge_ng(metric_name, value, labels)
                                else:
                                    metric_name = f"octopus_{self.strip_device_id(meter.device_id)}_{reading_type}"
                                    self.update_gauge(metric_name, value)
                        
                        meter.last_called = current_time
                
                time.sleep(60)  # Check every minute
                
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
            except Exception as e:
                logger.error(f"Error in read_meters: {e}")
                time.sleep(60)

    def strip_device_id(self, device_id):
        """Strip device ID formatting."""
        return device_id.replace("-", "").replace(":", "").lower()

    def interval_rate_check(self, interval):
        """Validate and adjust polling interval."""
        if interval > 1800:  # 30 minutes maximum
            logger.warning("Interval too high, setting to 1800 seconds (30 minutes)")
            return 1800
        if interval <= 60:
            logger.warning("Interval too low, likely to hit API ratelimits.")
            return interval
        return interval

    def initial_load(self):
        self.get_device_id()
        
        if not self.meters:
            logger.warning("No meters found. Check your configuration.")

    def run(self):
        """Main entry point to run the exporter."""
        try:
            self.initial_load()
            
            for meter in self.meters:
                logger.info(f"Starting to read {meter.meter_type} meter every {meter.polling_interval} seconds")
            
            self.start_prometheus_server()
            self.read_meters()
            
        except Exception as e:
            logger.error(f"Failed to start exporter: {e}")
            raise
