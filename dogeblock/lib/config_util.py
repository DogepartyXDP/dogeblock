import os
import sys
import shutil
import codecs
import logging
import ctypes.util
import configparser
import platform
import argparse
import urllib.request
import tarfile
import appdirs
import hashlib
import decimal

from dogeblock.lib import config

D = decimal.Decimal
logger = logging.getLogger(__name__)


# Set default values of command line arguments with config file
def add_config_arguments(arg_parser, config_args, default_config_file, config_file_arg_name='config_file'):
    cmd_args = arg_parser.parse_known_args()[0]

    config_file = getattr(cmd_args, config_file_arg_name, None)
    if not config_file:
        config_dir = appdirs.user_config_dir(appauthor=config.XDP_NAME, appname=config.APP_NAME, roaming=True)
        if not os.path.isdir(config_dir):
            os.makedirs(config_dir, mode=0o755)
        config_file = os.path.join(config_dir, default_config_file)

    # clean BOM
    BUFSIZE = 4096
    BOMLEN = len(codecs.BOM_UTF8)
    with codecs.open(config_file, 'r+b') as fp:
        chunk = fp.read(BUFSIZE)
        if chunk.startswith(codecs.BOM_UTF8):
            i = 0
            chunk = chunk[BOMLEN:]
            while chunk:
                fp.seek(i)
                fp.write(chunk)
                i += len(chunk)
                fp.seek(BOMLEN, os.SEEK_CUR)
                chunk = fp.read(BUFSIZE)
            fp.seek(-BOMLEN, os.SEEK_CUR)
            fp.truncate()

    logger.debug('Loading configuration file: `{}`'.format(config_file))
    configfile = configparser.SafeConfigParser(
        defaults=os.environ, allow_no_value=True, inline_comment_prefixes=('#', ';'))
    with codecs.open(config_file, 'r', encoding='utf8') as fp:
        configfile.readfp(fp)

    if 'Default' not in configfile:
        configfile['Default'] = {}

    # Initialize default values with the config file.
    for arg in config_args:
        key = arg[0][-1].replace('--', '')
        if 'action' in arg[1] and arg[1]['action'] == 'store_true' and key in configfile['Default']:
            arg[1]['default'] = configfile['Default'].getboolean(key)
        elif key in configfile['Default'] and configfile['Default'][key]:
            arg[1]['default'] = configfile['Default'][key]
        elif key in configfile['Default'] and arg[1].get('nargs', '') == '?' and 'const' in arg[1]:
            arg[1]['default'] = arg[1]['const']  # bit of a hack
        arg_parser.add_argument(*arg[0], **arg[1])


def generate_config_file(filename, config_args, known_config={}, overwrite=False):
    if not overwrite and os.path.exists(filename):
        return

    config_dir = os.path.dirname(os.path.abspath(filename))
    if not os.path.exists(config_dir):
        os.makedirs(config_dir, mode=0o755)

    config_lines = []
    config_lines.append('[Default]')
    config_lines.append('')

    for arg in config_args:
        key = arg[0][-1].replace('--', '')
        value = None

        if key in known_config:
            value = known_config[key]
        elif 'default' in arg[1]:
            value = arg[1]['default']

        if value is None:
            value = ''
        elif isinstance(value, bool):
            value = '1' if value else '0'
        elif isinstance(value, (float, D)):
            value = format(value, '.8f')

        if 'default' in arg[1] or value == '':
            key = '# {}'.format(key)

        config_lines.append('{} = {}\t\t\t\t# {}'.format(key, value, arg[1]['help']))

    with open(filename, 'w', encoding='utf8') as config_file:
        config_file.writelines("\n".join(config_lines))
    os.chmod(filename, 0o660)


def extract_dogecoincore_config():
    dogecoincore_config = {}

    # Figure out the path to the dogecoin.conf file
    if platform.system() == 'Darwin':
        doge_conf_file = os.path.expanduser('~/Library/Application Support/Dogecoin/')
    elif platform.system() == 'Windows':
        doge_conf_file = os.path.join(os.environ['APPDATA'], 'Dogecoin')
    else:
        doge_conf_file = os.path.expanduser('~/.dogecoin')
    doge_conf_file = os.path.join(doge_conf_file, 'dogecoin.conf')

    # Extract contents of dogecoin.conf to build service_url
    if os.path.exists(doge_conf_file):
        conf = {}
        with open(doge_conf_file, 'r') as fd:
            # Dogecoin Core accepts empty rpcuser, not specified in doge_conf_file
            for line in fd.readlines():
                if '#' in line or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                conf[k.strip()] = v.strip()

            config_keys = {
                'rpcport': 'backend-port',
                'rpcuser': 'backend-user',
                'rpcpassword': 'backend-password',
                'rpcssl': 'backend-ssl'
            }

            for dogecoind_key in config_keys:
                if dogecoind_key in conf:
                    dogeparty_key = config_keys[dogecoind_key]
                    dogecoincore_config[dogeparty_key] = conf[dogecoind_key]

    return dogecoincore_config


def extract_dogeparty_server_config():
    dogeparty_server_config = {}

    # Figure out the path to the server.conf file
    configdir = appdirs.user_config_dir(appauthor=config.XDP_NAME, appname=config.DOGEPARTY_APP_NAME, roaming=True)
    server_configfile = os.path.join(configdir, 'server.conf')

    # Extract contents of server.conf to build service_url
    if os.path.exists(server_configfile):
        conf = {}
        with open(server_configfile, 'r') as fd:
            for line in fd.readlines():
                if '#' in line or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                conf[k.strip()] = v.strip()

            config_keys = {
                'backend-connect': 'backend-connect',
                'backend-port': 'backend-port',
                'backend-user': 'backend-user',
                'backend-password': 'backend-password',
                'rpc-port': 'dogeparty-port',
                'rpc-user': 'dogeparty-user',
                'rpc-password': 'dogeparty-password',
            }

            for server_key in config_keys:
                if server_key in conf:
                    dogeparty_key = config_keys[server_key]
                    dogeparty_server_config[dogeparty_key] = conf[server_key]

    return dogeparty_server_config


def generate_config_files():
    from dogeblock.server import CONFIG_ARGS

    data_dir, config_dir, log_dir = config.get_dirs()
    if not os.path.isdir(data_dir):
        os.makedirs(data_dir)
    if not os.path.isdir(config_dir):
        os.makedirs(config_dir)
    if not os.path.isdir(log_dir):
        os.makedirs(log_dir)

    server_configfile = os.path.join(config_dir, 'server.conf')
    if not os.path.exists(server_configfile):
        # extract known configuration
        server_known_config = {}

        dogecoincore_config = extract_dogecoincore_config()
        server_known_config.update(dogecoincore_config)
        dogeparty_server_config = extract_dogeparty_server_config()
        server_known_config.update(dogeparty_server_config)

        generate_config_file(server_configfile, CONFIG_ARGS, server_known_config)
