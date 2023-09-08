# Copyright 2023 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

""" opergroup_bgp_sros.py
Script demonstrating how intelligence can be built into SROS EHS using pySROS
to decide when to shut down links based on BGP events generated by the node.
"""

import time
from datetime import datetime
from utime import localtime, strftime
from pysros.exceptions import SrosMgmtError
from pysros.management import connect
from pysros.ehs import get_event
from pysros.pprint import Table


WELL_KNOWN_EBGP_GROUP_NAME = "rr"
MIN_NUM_ACTIVE_BGP_SESSIONS = 1


def print_log(message):
    """
    Helper function to display log messages with a timestamp
    """
    curr_time = localtime()
    time_str = "%g/%g/%g %g:%g:%g %s" % (
        curr_time.tm_year,
        curr_time.tm_mon,
        curr_time.tm_mday,
        curr_time.tm_hour,
        curr_time.tm_min,
        curr_time.tm_sec,
        "CEST",
    )
    time_str = strftime("%Y/%m/%d %H:%M:%S", curr_time)
    format_str = "At time %s: %s" % (time_str, message)

    print(format_str)


def router_timestamp_to_datetime(router_timestamp_string):
    """
    Helper function to format a timestamp found in the router's state model
    to one that is more easily viewable for humans.
    """
    return datetime.fromisoformat(router_timestamp_string.data.split(".")[0])


def to_rows(active_interfaces):
    """
    Helper function to convert interfaces found that should be turned down
    into rows that contain relevant information for this script
    """
    for key, value in active_interfaces.items():
        now = datetime.now()
        last = router_timestamp_to_datetime(value["last-oper-change"])
        yield key, last, now - last


def print_table(rows, info, header):
    """
    Function to display the interfaces that will be modified both before and
    after the modifications are done.
    """
    cols = [
        (20, "Interface"),
        (40, "Last oper change"),
        (18, "%s since" % info),
    ]

    # Initalize the Table object with the heading and columns.
    table = Table(
        "Interfaces modified by script %s"
        % (header),
        cols,
        showCount="Interfaces",
    )

    # The rows are added as a list of lists. Each list item having
    # 3 items as that is how many columns we have in our table.
    table.print(rows)


def find_interfaces(connection, oper_status_filter=""):
    """
    Function that takes as input an optional filter and returns interfaces
    that have an operational state matching the given filter. The returned
    interfaces are in a dictionary
    """
    interface_filter = {"oper-state": oper_status_filter}
    base_router_state = connection.running.get(
        '/nokia-state:state/router[router-name="Base"]/interface',
        filter=interface_filter,
    )

    active_interfaces = {}
    for interface, state in base_router_state.items():
        if "tester" not in interface:
            # do not do anything to non-tester interfaces
            continue
        active_interfaces[interface] = state
    return active_interfaces


def modify_downstream_interfaces(connection, active_interfaces, change):
    """
    Passed a list of interfaces to disable, this function creates a
    NETCONF payload and sends it to the node to make the necessary changes
    for downstream nodes to no longer rely on this node.
    """
    assert change in ["enable", "disable"], "Action must be enable or disable. Found %s" % change
    print(
        "Interfaces to be brought %s:\n\t%s\n"
        % (("up" if change == "enable" else "down").lower(), "\n\t".join(active_interfaces.keys()))
    )

    cfg_path = '/nokia-conf:configure/router[router-name="Base"]'
    cfg_payload = {"nokia-conf:interface": {}}
    for interface in active_interfaces:
        cfg_payload["nokia-conf:interface"][interface] = {
            "interface-name": interface,
            "admin-state": change,
        }
    if cfg_payload["nokia-conf:interface"]:
        try:
            connection.candidate.set(cfg_path, cfg_payload)
        except SrosMgmtError as error:
            print(
                "Disabling the active interfaces failed due to an error:\n\t%s"
                % error.args
            )
        except TypeError as error:
            print(error)
            print(cfg_payload)
    return True


def count_ebgp_sessions_established(connection):
    """
    Function to count the number of active eBGP sessions, relying on
    global variables for direction as to which sessions are used for
    upstream traffic. If this counter drops below a defined threshold,
    downstream nodes should be protected.
    """
    nb_filter = {
        "statistics": {
            "received-paths": {},  # put something to get the whole context
            "session-state": "Established",
        }
    }
    # would use get_list_keys but can't as filter is needed
    # these are the eBGP sessions we have
    bgp_session_state = connection.running.get(
        '/nokia-state:state/router[router-name="Base"]/bgp/neighbor',
        filter=nb_filter,
    )

    ebgp_group_filter = {"ip-address": {}, "group": WELL_KNOWN_EBGP_GROUP_NAME}
    # these are the eBGP sessions we intend to have
    find_ebgp_configuration = connection.running.get(
        '/nokia-conf:configure/router[router-name="Base"]/bgp/neighbor',
        filter=ebgp_group_filter,
    )

    number_of_active_ebgp_sessions = 0
    for ebgp_session in find_ebgp_configuration.keys():
        if ebgp_session in bgp_session_state:
            number_of_active_ebgp_sessions += 1
    return number_of_active_ebgp_sessions


def backwardsHandler(connection, subject):
    """
    Handler function to abstract away duality of this script.
    When a BGP session fails, this could be the last BGP session
    on this node. This is verified and if that is indeed the case,
    downstream interfaces are shut down.
    """
    message = (
        "received a BGP session degradation event for BGP Peer :\n\t%s,\n"
        % (subject)
        + "We need at least a single uplink bgp peer! Checking other peerings."
    )
    print_log(message)
    active_ebgp_sessions = count_ebgp_sessions_established(connection)
    if active_ebgp_sessions >= MIN_NUM_ACTIVE_BGP_SESSIONS:
        message = (
            "Found %d active peerings, we need %d. Situation is manageable.\n"
            % (active_ebgp_sessions, MIN_NUM_ACTIVE_BGP_SESSIONS)
        )
        print_log(message)
    else:
        message = (
            "Found %d active peerings, we need %d. "
            "Protect the downstream network!\n"
            % (active_ebgp_sessions, MIN_NUM_ACTIVE_BGP_SESSIONS)
        )
        print_log(message)

        if active_interfaces := find_interfaces(connection, oper_status_filter="up"):
            print_table(to_rows(active_interfaces), "Up", "(up -> down) [BEFORE]")
            time.sleep(1)
            modify_downstream_interfaces(connection, active_interfaces, "disable")
            time.sleep(1)
            if inactive_interfaces := find_interfaces(
                connection, oper_status_filter="down"
            ):
                print_table(to_rows(inactive_interfaces), "Down", "(up -> down) [AFTER]")
        else:
            print("No changes required, interfaces already down.")

def establishedHandler(connection, subject):
    """
    Handler function to abstract away duality of this script.
    When a BGP session is established, this could be the first upstream
    BGP session on this node. This is verified,  if that is indeed the case,
    this script attempts to activate downstream interfaces.
    """
    message = (
        "received a BGP session establishment event for BGP Peer :\n\t%s,\n"
        % (subject)
    )
    print_log(message)
    active_ebgp_sessions = count_ebgp_sessions_established(connection)
    if active_ebgp_sessions >= MIN_NUM_ACTIVE_BGP_SESSIONS:
        message = (
            "Found %d active peerings, we need %d. Interfaces to be re-enabled.\n"
            % (active_ebgp_sessions, MIN_NUM_ACTIVE_BGP_SESSIONS)
        )
        active_interfaces = find_interfaces(
            connection, oper_status_filter="down"
        )
        if (active_interfaces):
            print_table(to_rows(active_interfaces), "Down", "(down -> up) [BEFORE]")
            time.sleep(1)
            modify_downstream_interfaces(connection, active_interfaces, "enable")
            time.sleep(1)
            if inactive_interfaces := find_interfaces(
                connection, oper_status_filter="up"
            ):
                print_table(to_rows(inactive_interfaces), "Up", "(down -> up) [AFTER]")
        else:
            print("No changes required, interfaces already up.")

def main():
    """The main procedure.  The execution starts here."""
    connection = connect(
        host="local connection only - unused",
        username="local connection only - unused",
    )

    trigger_event = get_event()
    if not trigger_event or trigger_event.subject[0].isdigit():
        connection.disconnect()
        return

    if trigger_event.eventid == 2039:
       backwardsHandler(connection, trigger_event.subject)
    elif trigger_event.eventid == 2038:
       establishedHandler(connection, trigger_event.subject)

    connection.disconnect()


if __name__ == "__main__":
    main()


# [/]
# A:admin@DCGW1# show _20230511-221649-UTC.844366.out
# File: _20230511-221649-UTC.844366.out
# -------------------------------------------------------------------------------
# At time 2023/05/11 22:16:49: received a BGP session degradation event for ...
#         Peer 1: 10.1.5.5,
# We need at least a single uplink to the Internet. Checking other peerings.
# At time 2023/05/11 22:16:49: Found 0 active peerings, we ...
#
# ===============================================================================
# Interfaces modified by script (up -> down) [BEFORE]
# ===============================================================================
# Interface            Last oper change                         Up since
# -------------------------------------------------------------------------------
# Spine8               2023-05-11 22:14:01                      0:02:48
# Spine7               2023-05-11 22:14:01                      0:02:48
# Spine6               2023-05-11 22:14:01                      0:02:48
# Spine9               2023-05-11 22:14:01                      0:02:48
# -------------------------------------------------------------------------------
# No. of Interfaces: 4
# ===============================================================================
# Interfaces to be brought down:
#         Spine8
#         Spine7
#         Spine6
#         Spine9
#
# ===============================================================================
# Interfaces modified by script (up -> down) [AFTER]
# ===============================================================================
# Interface            Last oper change                         Down since
# -------------------------------------------------------------------------------
# Spine8               2023-05-11 22:16:50                      0:00:03
# Spine7               2023-05-11 22:16:51                      0:00:02
# Spine6               2023-05-11 22:16:51                      0:00:02
# Spine9               2023-05-11 22:16:51                      0:00:02
# -------------------------------------------------------------------------------
# No. of Interfaces: 4
# ===============================================================================
#
# ===============================================================================
