from owrx.kiss import KissDeframer
from owrx.map import Map, LatLngLocation
from owrx.bands import Bandplan
import logging

logger = logging.getLogger(__name__)


def decodeBase91(input):
    base = decodeBase91(input[:-1]) * 91 if len(input) > 1 else 0
    return base + (ord(input[-1]) - 33)


class Ax25Parser(object):

    def parse(self, ax25frame):
        control_pid = ax25frame.find(bytes([0x03, 0xf0]))
        if control_pid % 7 > 0:
            logger.warning("aprs packet framing error: control/pid position not aligned with 7-octet callsign data")

        def chunks(l, n):
            """Yield successive n-sized chunks from l."""
            for i in range(0, len(l), n):
                yield l[i:i + n]

        return {
            "destination": self.extractCallsign(ax25frame[0:7]),
            "source": self.extractCallsign(ax25frame[7:14]),
            "path": [self.extractCallsign(c) for c in chunks(ax25frame[14:control_pid], 7)],
            "data": ax25frame[control_pid+2:]
        }

    def extractCallsign(self, input):
        cs = bytes([b >> 1 for b in input[0:6]]).decode('us-ascii').strip()
        ssid = (input[6] & 0b00011110) >> 1
        if ssid > 0:
            return "{callsign}-{ssid}".format(callsign=cs, ssid=ssid)
        else:
            return cs


class AprsParser(object):

    def __init__(self, handler):
        self.ax25parser = Ax25Parser()
        self.deframer = KissDeframer()
        self.dial_freq = None
        self.band = None
        self.handler = handler

    def setDialFrequency(self, freq):
        self.dial_freq = freq
        self.band = Bandplan.getSharedInstance().findBand(freq)

    def parse(self, raw):
        for frame in self.deframer.parse(raw):
            try:
                data = self.ax25parser.parse(frame)

                # TODO how can we tell if this is an APRS frame at all?
                aprsData = self.parseAprsData(data)

                logger.debug(aprsData)
                if "lat" in aprsData and "lon" in aprsData:
                    loc = LatLngLocation(aprsData["lat"], aprsData["lon"], aprsData["comment"] if "comment" in aprsData else None)
                    Map.getSharedInstance().updateLocation(data["source"], loc, "APRS", self.band)

                self.handler.write_aprs_data(aprsData)
            except Exception:
                logger.exception("exception while parsing aprs data")

    def hasCompressedCoordinatesx(self, raw):
        return raw[0] == "/" or raw[0] == "\\"

    def parseUncompressedCoordinates(self, raw):
        lat = int(raw[0:2]) + float(raw[2:7]) / 60
        if raw[7] == "S":
            lat *= -1
        lon = int(raw[9:12]) + float(raw[12:17]) / 60
        if raw[17] == "W":
            lon *= -1
        return {
            "lat": lat,
            "lon": lon,
            "symbol": raw[18]
        }

    def parseCompressedCoordinates(self, raw):
        return {
            "lat": 90 - decodeBase91(raw[1:5]) / 380926,
            "lon": -180 + decodeBase91(raw[5:9]) / 190463,
            "symbol": raw[9]
        }

    def parseAprsData(self, data):
        information = data["data"]

        # forward some of the ax25 data
        aprsData = {
            "source": data["source"],
            "destination": data["destination"],
            "path": data["path"]
        }

        if information[0] == 0x1c or information[0] == 0x60:
            parser = MicEParser()
            aprsData.update(parser.parse(data))
            return aprsData

        information = information.decode('us-ascii')
        logger.debug(information)

        if information[0] == "!" or information[0] == "=":
            # position without timestamp
            aprsData.update(self.parseRegularAprsData(information[1:]))
        elif information[0] == "/" or information[0] == "@":
            # position with timestamp
            # TODO parse timestamp
            aprsData.update(self.parseRegularAprsData(information[8:]))

        return aprsData

    def parseRegularAprsData(self, information):
        if self.hasCompressedCoordinatesx(information):
            aprsData = self.parseCompressedCoordinates(information[0:10])
            aprsData["comment"] = information[10:]
        else:
            aprsData = self.parseUncompressedCoordinates(information[0:19])
            aprsData["comment"] = information[19:]
        return aprsData


class MicEParser(object):
    def extractNumber(self, input):
        n = ord(input)
        if n >= ord("P"):
            return n - ord("P")
        if n >= ord("A"):
            return n - ord("A")
        return n - ord("0")

    def listToNumber(self, input):
        base = self.listToNumber(input[:-1]) * 10 if len(input) > 1 else 0
        return base + input[-1]

    def extractAltitude(self, comment):
        if len(comment) < 4 or comment[3] != "}":
            return (comment, None)
        return comment[4:], decodeBase91(comment[:3]) - 10000

    def extractDevice(self, comment):
        if comment[0] == ">":
            if comment[-1] == "=":
                return comment[1:], {"manufacturer": "Kenwood", "device": "TH-D72"}
            if comment[-1] == "^":
                return comment[1:], {"manufacturer": "Kenwood", "device": "TH-D74"}
            return comment[1:], {"manufacturer": "Kenwood", "device": "TH-D7A"}
        if comment[0] == "]":
            if comment[-1] == "=":
                return comment[1:], {"manufacturer": "Kenwood", "device": "TM-D710"}
            return comment[1:], {"manufacturer": "Kenwood", "device": "TM-D700"}
        if comment[0] == "`" or comment[0] == "'":
            if comment[-2] == "_":
                devices = {
                    "b":  "VX-8",
                    "\"": "FTM-350",
                    "#":  "VX-8G",
                    "$":  "FT1D",
                    "%":  "FTM-400DR",
                    ")":  "FTM-100D",
                    "(":  "FT2D",
                    "0":  "FT3D",
                }
                return comment[1:-2], {"manufacturer": "Yaesu", "device": devices.get(comment[-1], "Unknown")}
            if comment[-2:] == " X":
                return comment[1:-2], {"manufacturer": "SainSonic", "device": "AP510"}
            if comment[-2] == "(":
                devices = {
                    "5": "D578UV",
                    "8": "D878UV"
                }
                return comment[1:-2], {"manufacturer": "Anytone", "device": devices.get(comment[-1], "Unknown")}
            if comment[-2] == "|":
                devices = {
                    "3": "TinyTrack3",
                    "4": "TinyTrack4"
                }
                return comment[1:-2], {"manufacturer": "Byonics", "device": devices.get(comment[-1], "Unknown")}
            if comment[-2:] == "^v":
                return comment[1:-2], {"manufacturer": "HinzTec", "device": "anyfrog"}
            if comment[-2] == ":":
                devices = {
                    "4": "P4dragon DR-7400 modem",
                    "8": "P4dragon DR-7800 modem"
                }
                return comment[1:-2], {"manufacturer": "SCS GmbH & Co.", "device": devices.get(comment[-1], "Unknown")}
            if comment[-2:] == "~v":
                return comment[1:-2], {"manufacturer": "Other", "device": "Other"}
            return comment[1:-2], None
        return comment, None

    def parse(self, data):
        information = data["data"]
        destination = data["destination"]

        logger.debug(destination)
        rawLatitude = [self.extractNumber(c) for c in destination[0:6]]
        logger.debug(rawLatitude)
        lat = self.listToNumber(rawLatitude[0:2]) + self.listToNumber(rawLatitude[2:6]) / 6000
        if ord(destination[3]) <= ord("9"):
            lat *= -1

        logger.debug(lat)

        logger.debug(information)
        lon = information[1] - 28
        if ord(destination[4]) >= ord("P"):
            lon += 100
        if 180 <= lon <= 189:
            lon -= 80
        if 190 <= lon <= 199:
            lon -= 190

        minutes = information[2] - 28
        if minutes >= 60:
            minutes -= 60

        lon += minutes / 60 + (information[3] - 28) / 6000

        if ord(destination[5]) >= ord("P"):
            lon *= -1

        comment = information[9:].decode('us-ascii').strip()
        (comment, altitude) = self.extractAltitude(comment)

        (comment, device) = self.extractDevice(comment)

        # altitude might be inside the device string, so repeat and choose one
        (comment, insideAltitude) = self.extractAltitude(comment)
        altitude = next((a for a in [altitude, insideAltitude] if a is not None), None)

        return {
            "lat": lat,
            "lon": lon,
            "comment": comment,
            "altitude": altitude,
            "device": device,
        }
