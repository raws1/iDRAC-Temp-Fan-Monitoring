#!/bin/bash
#the IP address of iDrac
IPMIHOST="${POWEREDGE_SHUTUP_IPMIHOST:-${IDRAC_HOST:-}}"

#iDrac user
IPMIUSER="${POWEREDGE_SHUTUP_IPMIUSER:-${IDRAC_USER:-}}"

#iDrac password
IPMIPW="${POWEREDGE_SHUTUP_IPMIPW:-${IDRAC_PASSWORD:-}}"

#YOUR IPMI ENCRYPTION KEY
IPMIEK="${POWEREDGE_SHUTUP_IPMIEK:-${IDRAC_ENCRYPTION_KEY:-0000000000000000000000000000000000000000}}"

#Side note: you shouldn't ever store credentials in a script. Period. Here it's an example. 
#I suggest you give a look at tools like https://github.com/plyint/encpass.sh 

for required_var in IPMIHOST IPMIUSER IPMIPW; do
    if [[ -z "${!required_var}" ]]; then
        echo "Missing required setting: ${required_var}" >&2
        exit 1
    fi
done

ipmitool -I lanplus -H $IPMIHOST -U $IPMIUSER -P $IPMIPW -y $IPMIEK sdr type temperature

# Should return something like that:
#Inlet Temp | 04h | ok | 7.1 | 19 degrees C
#Exhaust Temp | 01h | ok | 7.1 | 36 degrees C
#Temp | 0Eh | ok | 3.1 | 41 degrees C
#Temp | 0Fh | ok | 3.2 | 40 degrees C

#It lets you know the "id" to grep for in the fancontrol.sh script.
