#!/bin/bash

# Run "setup.py develop" if we need to
if [ ! -d /dogeblock/dogeblock.egg-info ]; then
    cd /dogeblock; python3 setup.py develop; cd /
fi

# Launch, utilizing the SIGTERM/SIGINT propagation pattern from
# http://veithen.github.io/2014/11/16/sigterm-propagation.html
: ${PARAMS:=""}
: ${COMMAND:="server"}
trap 'kill -TERM $PID' TERM INT
dogeblock ${PARAMS} ${COMMAND} &
PID=$!
wait $PID
trap - TERM INT
wait $PID
EXIT_STATUS=$?
