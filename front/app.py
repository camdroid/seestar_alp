import time
from datetime import datetime

import falcon
from falcon import HTTPTemporaryRedirect, HTTPFound
from astroquery.simbad import Simbad
from jinja2 import Template, Environment, FileSystemLoader
from wsgiref.simple_server import WSGIRequestHandler, make_server
import requests
import json
import re
import toml
import os

base_url = "http://localhost:5555"
messages = []


def flash(resp, message):
    # todo : set to internal state so it can be used!
    resp.set_cookie('flash_cookie', message, path='/')
    messages.append(message)

def get_messages():
    if len(messages) > 0:
        resp = messages
        messages.clear()
        return resp
    return []

def get_telescopes():
    with open('../device/config.toml', 'r') as inf:
        config = toml.load(inf)
        telescopes = config['seestars']
        return list(telescopes)


def get_telescope(telescope_id):
    telescopes = get_telescopes()
    return list(filter(lambda telescope: telescope['device_num'] == telescope_id, telescopes))[0]


def get_root(telescope_id):
    if telescope_id:
        telescopes = get_telescopes()
        # if len(telescopes) == 1:
        #     return ""

        telescope = list(filter(lambda tel: tel['device_num'] == telescope_id, telescopes))[0]
        if telescope:
            root = f"/{telescope['device_num']}"
            return root
    return ""


def get_context(telescope_id, req):
    # probably a better way of doing this...
    telescope = get_telescope(telescope_id)
    telescopes = get_telescopes()
    root = get_root(telescope_id)
    partial_path = "/".join(req.relative_uri.split("/", 2)[2:])
    return {"telescope": telescope, "telescopes": telescopes, "root": root, "partial_path": partial_path}


def get_flash_cookie(req, resp):
    cookie = req.get_cookie_values('flash_cookie')
    if cookie:
        resp.unset_cookie('flash_cookie', path='/')
        return cookie
    return []


def do_action_device(action, dev_num, parameters):
    url = f"{base_url}/api/v1/telescope/{dev_num}/action"
    payload = {
        "Action": action,
        "Parameters": json.dumps(parameters),
        "ClientID": 1,
        "ClientTransactionID": 999
    }
    r = requests.put(url, json=payload)
    return r.json()


def do_schedule_action_device(action, parameters, dev_num):
    if parameters:
        return do_action_device("add_schedule_item", dev_num, {
            "action": action,
            "params": parameters
        })
    else:
        return do_action_device("add_schedule_item", dev_num, {
            "action": action
        })


def check_response(resp, response):
    v = response["Value"]
    if isinstance(v, str):
        flash(resp, v)
    elif response["ErrorMessage"] != '':
        flash(resp, response["ErrorMessage"])
        # flash("Schedule item added successfully", "success")
    else:
        flash(resp, "Item scheduled successfully")


def method_sync(method, telescope_id=1):
    out = do_action_device("method_sync", telescope_id, {"method": method})

    if out["Value"].get("error"):
        return out["Value"]["error"]
    else:
        return out["Value"]["result"]


def get_device_state(telescope_id=1):
    result = method_sync("get_device_state", telescope_id)
    schedule = do_action_device("get_schedule", telescope_id, {})
    device = result["device"]
    settings = result["setting"]
    pi_status = result["pi_status"]
    stats = {
        "Firmware Version": device["firmware_ver_string"],
        "Focal Position": settings["focal_pos"],
        "Auto Power Off": settings["auto_power_off"],
        "Heater?": settings["heater_enable"],
        "Free Storage (MB)": result["storage"]["storage_volume"][0]["freeMB"],
        "Balance Sensor (angle)": result["balance_sensor"]["data"]["angle"],
        "Compass Sensor (direction)": result["compass_sensor"]["data"]["direction"],
        "Temperature Sensor": pi_status["temp"],
        "Charge Status": pi_status["charger_status"],
        "Battery %": pi_status["battery_capacity"],
        "Battery Temp": pi_status["battery_temp"],
        "Scheduler Status": schedule["Value"]["state"]
    }
    return stats


def get_telescopes_state():
    telescopes = get_telescopes()

    return list(map(lambda telescope: telescope | {"stats": get_device_state(telescope["device_num"])}, telescopes))


def check_ra_value(raString):
    valid = [
        r"^\d+h\s*\d+m\s*([0-9.]+s)?$",
        r"^\d+(\.\d+)?$",
        r"^\d+\s+\d+\s+[0-9.]+$",
        r"^-1(\.0+)?$",
    ]
    return any(re.search(pattern, raString) for pattern in valid)


def check_dec_value(decString):
    valid = [
        r"^[+-]?\d+d\s*\d+m\s*([0-9.]+s)?$",
        r"^[+-]?\d+(\.\d+)?$",
        r"^[+-]?\d+\s+\d+\s+[0-9.]+$"
    ]
    return any(re.search(pattern, decString) for pattern in valid)


def do_create_mosaic(req, resp, schedule, telescope_id):
    form = req.media
    targetName = form["targetName"]
    ra, raPanels = form["ra"], form["raPanels"]
    dec, decPanels = form["dec"], form["decPanels"]
    panelOverlap = form["panelOverlap"]
    useJ2000 = form.get("useJ2000") == "on"
    sessionTime = form["sessionTime"]
    useLpfilter = form.get("useLpFilter") == "on"
    useAutoFocus = form.get("useAutoFocus") == "on"
    gain = form["gain"]
    errors = {}
    values = {
        "target_name": targetName,
        "is_j2000": useJ2000,
        "ra": ra,
        "dec": dec,
        "is_use_lp_filter": useLpfilter,
        "session_time_sec": int(sessionTime),
        "ra_num": int(raPanels),
        "dec_num": int(decPanels),
        "panel_overlap_percent": int(panelOverlap),
        "gain": int(gain),
        "is_use_autofocus": useAutoFocus
    }

    if not check_ra_value(ra):
        flash(resp, "Invalid RA value")
        errors["ra"] = ra

    if not check_dec_value(dec):
        flash(resp, "Invalid DEC Value")
        errors["dec"] = dec

    if errors:
        flash(resp, "ERROR detected in Coordinates")
        return values, errors

    if schedule:
        response = do_action_device("add_schedule_item", telescope_id, {
            "action": "start_mosaic",
            "params": values
        })
        print("POST scheduled request", values, response)
        check_response(resp, response)
    else:
        response = do_action_device("start_mosaic", telescope_id, values)
        print("POST immediate request", values, response)

    return values, errors


def do_create_image(req, resp, schedule, telescope_id):
    form = req.media
    targetName = form["targetName"]
    ra, raPanels = form["ra"], 1
    dec, decPanels = form["dec"], 1
    panelOverlap = 100
    useJ2000 = form.get("useJ2000") == "on"
    sessionTime = form["sessionTime"]
    useLpfilter = form.get("useLpFilter") == "on"
    useAutoFocus = form.get("useAutoFocus") == "on"
    gain = form["gain"]
    errors = {}
    values = {
        "target_name": targetName,
        "is_j2000": useJ2000,
        "ra": ra,
        "dec": dec,
        "is_use_lp_filter": useLpfilter,
        "session_time_sec": int(sessionTime),
        "ra_num": int(raPanels),
        "dec_num": int(decPanels),
        "panel_overlap_percent": int(panelOverlap),
        "gain": int(gain),
        "is_use_autofocus": useAutoFocus
    }

    if not check_ra_value(ra):
        flash(resp, "Invalid RA value")
        errors["ra"] = ra

    if not check_dec_value(dec):
        flash(resp, "Invalid DEC Value")
        errors["dec"] = dec

    if errors:
        flash(resp, "ERROR detected in Coordinates")
        return values, errors

    # print("values:", values)
    if schedule:
        response = do_action_device("add_schedule_item", telescope_id, {
            "action": "start_mosaic",
            "params": values
        })
        print("POST scheduled request", values, response)
        check_response(resp, response)
    else:
        response = do_action_device("start_mosaic", telescope_id, values)
        print("POST immediate request", values, response)

    return values, errors


def do_command(req, resp, telescope_id):
    form = req.media
    # print("Form: ", form)
    value = form["command"]
    # print ("Selected command: ", value)
    match value:
        case "start_up_sequence":
            output = do_action_device("action_start_up_sequence", telescope_id, {"lat": 0, "lon": 0})
            return None
        case "scope_park":
            output = method_sync("scope_park", telescope_id)
            return None
        case "pi_reboot":
            output = method_sync("pi_reboot", telescope_id)
            return None
        case "pi_shutdown":
            output = method_sync("pi_shutdown", telescope_id)
            return None
        case "start_auto_focus":
            output = method_sync("start_auto_focus", telescope_id)
            return None
        case "stop_auto_focus":
            output = method_sync("stop_auto_focus", telescope_id)
            return None
        case "get_focuser_position":
            output = method_sync("get_focuser_position", telescope_id)
            return output
        case "get_last_focuser_position":
            output = method_sync("get_focuser_position", telescope_id)
            return output
        case "start_solve":
            output = method_sync("start_solve", telescope_id)
            return None
        case "get_solve_result":
            output = method_sync("get_solve_result", telescope_id)
            return output
        case "get_last_solve_result":
            output = method_sync("get_last_solve_result", telescope_id)
            return output
        case "get_wheel_position":
            output = method_sync("get_wheel_position", telescope_id)
            return output
        case "get_wheel_state":
            output = method_sync("get_wheel_state", telescope_id)
            return output
        case "get_wheel_setting":
            output = method_sync("get_wheel_setting", telescope_id)
            return output
        case _:
            print("No command found")
    # print ("Output: ", output)


def redirect(location):
    raise HTTPFound(location)
    # raise HTTPTemporaryRedirect(location)


def render_template(req, resp, template_name, **context):
    template = Environment(loader=FileSystemLoader('./templates')).get_template(template_name)

    resp.status = falcon.HTTP_200
    resp.content_type = 'text/html'
    resp.text = template.render(flashed_messages=get_flash_cookie(req, resp),
                                messages=get_messages(),
                                **context)


def render_schedule_tab(req, resp, telescope_id, template_name, tab, values, errors):
    current = do_action_device("get_schedule", telescope_id, {})
    schedule = current["Value"]["list"]
    context = get_context(telescope_id, req)
    render_template(req, resp, template_name, schedule=schedule, tab=tab, errors=errors, values=values,
                    **context)


class HomeResource:
    @staticmethod
    def on_get(req, resp):
        now = datetime.now()
        telescopes = get_telescopes_state()
        telescope = telescopes[0]  # We just force it to first telescope
        if len(telescopes) > 1:
            redirect(f"/{telescope['device_num']}/")
        else:
            root = get_root(telescope['device_num'])
            render_template(req, resp, 'index.html', now=now, telescopes=telescopes, telescope=telescope)


class HomeTelescopeResource:
    @staticmethod
    def on_get(req, resp, telescope_id):
        now = datetime.now()
        telescopes = get_telescopes_state()
        telescope = get_telescope(telescope_id)
        root = get_root(telescope_id)
        render_template(req, resp, 'index.html', now=now, telescopes=telescopes, telescope=telescope, root=root)


class ImageResource:
    def on_get(self, req, resp, telescope_id=1):
        values = {}
        self.image(req, resp, {}, {}, telescope_id)

    def on_post(self, req, resp, telescope_id=1):
        values, errors = do_create_image(req, resp, True, telescope_id)
        self.image(req, resp, values, errors, telescope_id)

    @staticmethod
    def image(req, resp, values, errors, telescope_id):
        current = do_action_device("get_schedule", telescope_id, {})
        state = current["Value"]["state"]
        schedule = current["Value"]["list"]
        context = get_context(telescope_id, req)
        # remove values=values to stop remembering values
        render_template(req, resp, 'image.html', state=state, schedule=schedule, values=values, errors=errors,
                        action=f"/{telescope_id}/image", **context)


class CommandResource:
    def on_get(self, req, resp, telescope_id=1):
        self.command(req, resp, telescope_id, {})

    def on_post(self, req, resp, telescope_id=1):
        output = do_command(req, resp, telescope_id)
        self.command(req, resp, telescope_id, output)

    @staticmethod
    def command(req, resp, telescope_id, output):
        current = do_action_device("get_schedule", telescope_id, {})
        state = current["Value"]["state"]
        schedule = current["Value"]["list"]
        context = get_context(telescope_id, req)

        render_template(req, resp, 'command.html', state=state, schedule=schedule, action=f"/{telescope_id}/command",
                        output=output, **context)


class MosaicResource:
    def on_get(self, req, resp, telescope_id=1):
        self.mosaic(req, resp, {}, {}, telescope_id)

    def on_post(self, req, resp, telescope_id=1):
        values, errors = do_create_mosaic(req, resp, False, telescope_id)
        self.mosaic(req, resp, values, errors, telescope_id)

    @staticmethod
    def mosaic(req, resp, values, errors, telescope_id):
        current = do_action_device("get_schedule", telescope_id, {})
        state = current["Value"]["state"]
        schedule = current["Value"]["list"]
        context = get_context(telescope_id, req)
        # remove values=values to stop remembering values
        render_template(req, resp, 'mosaic.html', state=state, schedule=schedule, values=values, errors=errors,
                        action=f"/{telescope_id}/mosaic", **context)


class ScheduleResource:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        render_schedule_tab(req, resp, telescope_id, 'schedule_wait_until.html', 'wait-until', {}, {})

    @staticmethod
    def on_post(req, resp, telescope_id=1):
        render_schedule_tab(req, resp, telescope_id, 'schedule_wait_until.html', 'wait-until', {}, {})


class ScheduleWaitUntilResource:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        render_schedule_tab(req, resp, telescope_id, 'schedule_wait_until.html', 'wait-until', {}, {})

    @staticmethod
    def on_post(req, resp, telescope_id=1):
        waitUntil = req.media["waitUntil"]
        response = do_schedule_action_device("wait_until", {"local_time": waitUntil}, telescope_id)
        # print("POST scheduled request", response)
        check_response(resp, response)
        render_schedule_tab(req, resp, telescope_id, 'schedule_wait_until.html', 'wait-until', {}, {})


class ScheduleWaitForResource:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        render_schedule_tab(req, resp, telescope_id, 'schedule_wait_for.html', 'wait-for', {}, {})

    @staticmethod
    def on_post(req, resp, telescope_id=1):
        waitFor = req.media["waitFor"]
        response = do_schedule_action_device("wait_for", {"timer_sec": int(waitFor)}, telescope_id)
        # print("POST scheduled request", response)
        check_response(resp, response)
        render_schedule_tab(req, resp, telescope_id, 'schedule_wait_for.html', 'wait-for', {}, {})


class ScheduleAutoFocusResource:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        render_schedule_tab(req, resp, telescope_id, 'schedule_auto_focus.html', 'auto-focus', {}, {})

    @staticmethod
    def on_post(req, resp, telescope_id=1):
        autoFocus = req.media["autoFocus"]
        response = do_schedule_action_device("auto_focus", {"try_count": int(autoFocus)}, telescope_id)
        print("POST scheduled request", response)
        check_response(resp, response)
        render_schedule_tab(req, resp, telescope_id, 'schedule_auto_focus.html', 'auto-focus', {}, {})


class ScheduleImageResource:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        render_schedule_tab(req, resp, telescope_id, 'schedule_image.html', 'image', {}, {})

    @staticmethod
    def on_post(req, resp, telescope_id=1):
        values, errors = do_create_image(req, resp, True, telescope_id)
        render_schedule_tab(req, resp, telescope_id, 'schedule_image.html', 'image', values, errors)


class ScheduleMosaicResource:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        render_schedule_tab(req, resp, telescope_id, 'schedule_mosaic.html', 'mosaic', {}, {})

    @staticmethod
    def on_post(req, resp, telescope_id=1):
        values, errors = do_create_mosaic(req, resp, True, telescope_id)
        render_schedule_tab(req, resp, telescope_id, 'schedule_mosaic.html', 'mosaic', values, errors)


class ScheduleShutdownResource:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        render_schedule_tab(req, resp, telescope_id, 'schedule_shutdown.html', 'shutdown', {}, {})

    @staticmethod
    def on_post(req, resp, telescope_id=1):
        response = do_schedule_action_device("shutdown", "", telescope_id)
        check_response(resp, response)
        render_schedule_tab(req, resp, telescope_id, 'schedule_shutdown.html', 'shutdown', {}, {})


class ScheduleToggleResource:
    def on_get(self, req, resp, telescope_id=1):
        self.display_state(req, resp, telescope_id)

    def on_post(self, req, resp, telescope_id=1):
        current = do_action_device("get_schedule", telescope_id, {})
        state = current["Value"]["state"]
        if state == "Stopped":
            do_action_device("start_scheduler", telescope_id, {})
        else:
            do_action_device("stop_scheduler", telescope_id, {})
        self.display_state(req, resp, telescope_id)

    @staticmethod
    def display_state(req, resp, telescope_id):
        current = do_action_device("get_schedule", telescope_id, {})
        state = current["Value"]["state"]
        context = get_context(telescope_id, req)
        render_template(req, resp, 'partials/schedule_state.html', state=state, **context)


class ScheduleClearResource:
    @staticmethod
    def on_post(req, resp, telescope_id=1):
        current = do_action_device("get_schedule", telescope_id, {})
        state = current["Value"]["state"]
        if state == "Running":
            do_action_device("stop_scheduler", telescope_id, {})
            flash(resp, "Stopping scheduler")

        do_action_device("create_schedule", telescope_id, {})
        flash(resp, "Created New Schedule")
        redirect(f"/{telescope_id}/schedule")


class LivePage:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        status = method_sync('get_view_state')
        print(status)
        view_state = "Idle"
        mode = ""
        if status.get("View"):
            view_state = status["View"]["state"]
            mode = status["View"]["mode"]
        state = method_sync("get_device_state")
        # ip = state["station"]["ip"]
        context = get_context(telescope_id, req)
        render_template(req, resp, 'live.html', mode=f"{view_state} {mode}.", **context)
        # Of non-star mode, offer to open link:  stream=rtps://{ip}:4554/stream


class SearchObjectResource:
    @staticmethod
    def on_post(req, resp):
        result_table = Simbad.query_object(req.media["q"])
        print(result_table)
        if result_table and len(result_table) > 0:
            row = result_table[0]
            resp.media = {
                "name": row.get("MAIN_ID"),
                "ra": row.get("RA"),
                "dec": row.get("DEC"),
            }
        else:
            resp.media = {}


class SettingsResource:
    @staticmethod
    def on_get(req, resp):
        render_template(req, resp, 'settings.html')


class StatsResource:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        stats = get_device_state()
        now = datetime.now()
        context = get_context(telescope_id, req)
        render_template(req, resp, 'stats.html', stats=stats, now=now, **context)


class LoggingWSGIRequestHandler(WSGIRequestHandler):
    """Subclass of  WSGIRequestHandler allowing us to control WSGI server's logging"""

    def log_message(self, format: str, *args):
        # if args[1] != '200':  # Log this only on non-200 responses
        print(f'{datetime.now()} {self.client_address[0]} <- {format % args}')


def main():
    app = falcon.App()
    app.add_route('/', HomeResource())
    app.add_route('/image', ImageResource())
    app.add_route('/live', LivePage())
    app.add_route('/mosaic', MosaicResource())
    app.add_route('/search', SearchObjectResource())
    app.add_route('/settings', SettingsResource())
    app.add_route('/schedule', ScheduleResource())
    app.add_route('/command', CommandResource())
    app.add_route('/schedule/clear', ScheduleClearResource())
    app.add_route('/schedule/image', ScheduleImageResource())
    app.add_route('/schedule/mosaic', ScheduleMosaicResource())
    app.add_route('/schedule/shutdown', ScheduleShutdownResource())
    app.add_route('/schedule/state', ScheduleToggleResource())
    app.add_route('/schedule/wait-until', ScheduleWaitUntilResource())
    app.add_route('/schedule/wait-for', ScheduleWaitForResource())
    app.add_route('/schedule/auto-focus', ScheduleAutoFocusResource())
    app.add_route('/stats', StatsResource())
    app.add_route('/{telescope_id:int}/', HomeTelescopeResource())
    app.add_route('/{telescope_id:int}/command', CommandResource())
    app.add_route('/{telescope_id:int}/image', ImageResource())
    app.add_route('/{telescope_id:int}/live', LivePage())
    app.add_route('/{telescope_id:int}/mosaic', MosaicResource())
    app.add_route('/{telescope_id:int}/search', SearchObjectResource())
    app.add_route('/{telescope_id:int}/settings', SettingsResource())
    app.add_route('/{telescope_id:int}/schedule', ScheduleResource())
    app.add_route('/{telescope_id:int}/schedule/auto-focus', ScheduleAutoFocusResource())
    app.add_route('/{telescope_id:int}/schedule/clear', ScheduleClearResource())
    app.add_route('/{telescope_id:int}/schedule/image', ScheduleImageResource())
    app.add_route('/{telescope_id:int}/schedule/mosaic', ScheduleMosaicResource())
    app.add_route('/{telescope_id:int}/schedule/shutdown', ScheduleShutdownResource())
    app.add_route('/{telescope_id:int}/schedule/state', ScheduleToggleResource())
    app.add_route('/{telescope_id:int}/schedule/wait-until', ScheduleWaitUntilResource())
    app.add_route('/{telescope_id:int}/schedule/wait-for', ScheduleWaitForResource())
    app.add_route('/{telescope_id:int}/schedule', ScheduleResource())
    app.add_route('/{telescope_id:int}/stats', StatsResource())
    app.add_static_route("/public", f"{os.getcwd()}/public")
    try:
        # with make_server(Config.ip_address, Config.port, falc_app, handler_class=LoggingWSGIRequestHandler) as httpd:
        with make_server("127.0.0.1", 5432, app, handler_class=LoggingWSGIRequestHandler) as httpd:
            # logger.info(f'==STARTUP== Serving on {Config.ip_address}:{Config.port}. Time stamps are UTC.')
            # Serve until process is killed
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("Keyboard interupt. Server shutting down.")
        # for dev in Config.seestars:
        #     telescope.end_seestar_device(dev['device_num'])
        httpd.server_close()


if __name__ == '__main__':
    main()
