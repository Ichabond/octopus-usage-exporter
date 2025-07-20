"""Command-line interface for Octopus Energy Usage Exporter."""

import click
import logging
from . import __version__
from .exporter import OctopusEnergyExporter

# Configure logging
logger = logging.getLogger(__name__)

@click.command()
@click.option("--api-key", required=True, envvar="API_KEY", help="Octopus Energy API key")
@click.option("--account-number", required=True, envvar="ACCOUNT_NUMBER", help="Octopus Energy account number")
@click.option("--prom-port", default=9120, type=int, envvar="PROM_PORT", help="Prometheus port")
@click.option("--interval", default=1800, type=int, envvar="INTERVAL", help="Polling interval in seconds")
@click.option("--gas", default=False, type=bool, envvar="GAS", help="Enable gas meter monitoring")
@click.option("--electric", default=False, type=bool, envvar="ELECTRIC", help="Enable electric meter monitoring")
@click.option("--ng-metrics", default=False, type=bool, envvar="NG_METRICS", help="Enable next-generation metrics")
@click.option("--tariff-rates", default=False, type=bool, envvar="TARIFF_RATES", help="Enable tariff rate monitoring")
@click.option("--tariff-remaining", default=False, type=bool, envvar="TARIFF_REMAINING", help="Enable tariff remaining monitoring")
@click.option("--logging_level", default="INFO", type=str, envvar="LOGGING_LEVEL", help="Logging level")
@click.version_option(version=__version__)
def main(api_key, account_number, prom_port, interval, gas, electric, ng_metrics, tariff_rates, tariff_remaining, logging_level):
    """Octopus Energy Usage Exporter - Export energy usage data to Prometheus metrics."""
    logging.basicConfig(level=logging_level)
    logger.info(f"Octopus Energy Exporter by JRP - Version {__version__}")
    exporter = OctopusEnergyExporter(
        api_key=api_key,
        account_number=account_number,
        prom_port=prom_port,
        interval=interval,
        gas=gas,
        electric=electric,
        ng_metrics=ng_metrics,
        tariff_rates=tariff_rates,
        tariff_remaining=tariff_remaining
    )
    
    exporter.run()


if __name__ == "__main__":
    main()
