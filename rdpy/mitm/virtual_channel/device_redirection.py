import logging
import os
from io import BytesIO
from typing import Dict

from rdpy.core.layer import Layer
from rdpy.core.observer import Observer
from rdpy.enum.core import ParserMode
from rdpy.enum.virtual_channel.device_redirection import FileAccess, CreateOption, MajorFunction, \
    IOOperationSeverity
from rdpy.parser.rdp.virtual_channel.device_redirection import DeviceRedirectionParser
from rdpy.pdu.rdp.virtual_channel.device_redirection import DeviceIOResponsePDU, DeviceCreateRequestPDU, \
    DeviceCloseRequestPDU, DeviceReadRequestPDU, DeviceIORequestPDU, DeviceRedirectionPDU, \
    DeviceListAnnounceRequest
from rdpy.recording.recorder import Recorder


class PassiveDeviceRedirectionObserver(Observer):
    """
    The passive device redirection observer parses specific packets in the RDPDR channel to intercept
    and reconstruct transferred files. They are then saved to {currentDir}/saved_files/{filePath}
    as soon as it's done being transferred.
    """

    def __init__(self, layer: Layer, recorder: Recorder, mode: ParserMode, **kwargs):
        super().__init__(**kwargs)
        self.peer: PassiveDeviceRedirectionObserver = None
        self.layer = layer
        self.recorder = recorder
        self.mitm_log = logging.getLogger("mitm.deviceRedirection.{}"
                                          .format("client" if mode == ParserMode.CLIENT else "server"))
        self.deviceRedirectionParser = DeviceRedirectionParser()
        self.completionIdInProgress: Dict[MajorFunction, DeviceIORequestPDU] = {}
        self.reconstructedFilesTemp: Dict[int, BytesIO] = {}
        self.openedFiles: Dict[int, bytes] = {}
        self.finalFiles: Dict[str, BytesIO] = {}
        self.pduToSend = None  # Needed since the PDU changes if it's a response.

    def onPDUReceived(self, pdu: DeviceRedirectionPDU):
        """
        Handles the PDU and transfer it to the other end of the MITM.
        """
        self.pduToSend = pdu
        if isinstance(pdu, DeviceIORequestPDU):
            self.dealWithRequest(pdu)
        elif isinstance(pdu, DeviceIOResponsePDU):
            self.dealWithResponse(pdu)
        elif isinstance(pdu, DeviceListAnnounceRequest):
            [self.mitm_log.info(f"{device.deviceType.name} mapped with ID {device.deviceId}: {device.deviceData}") for device in pdu.deviceList]
        else:
            self.mitm_log.debug(f"Received unparsed PDU: {pdu.packetId.name}")

        self.peer.sendPDU(self.pduToSend)

    def dealWithRequest(self, pdu: DeviceIORequestPDU):
        """
        Sets the request in the list of requests in progress of the other end of the MITM.
        Also logs useful information.
        """
        self.peer.completionIdInProgress[pdu.completionId] = pdu
        if isinstance(pdu, DeviceReadRequestPDU):
            self.mitm_log.debug(f"ReadRequest received for file {self.peer.openedFiles[pdu.fileId]}")
        elif isinstance(pdu, DeviceCreateRequestPDU):
            if pdu.desiredAccess & (FileAccess.GENERIC_READ | FileAccess.FILE_READ_DATA):
                self.mitm_log.debug(f"Create request for read received for path {self.bytesToPath(pdu.path)}")
        else:
            self.mitm_log.debug(f"Unparsed request: {pdu.majorFunction}")

    def dealWithResponse(self, pdu: DeviceIOResponsePDU):
        """
        Based on the type of request the response is meant for, handle open files, closed files and read data.
        Also remove the associated request from the list of requests in progress.
        """
        if pdu.completionId in self.completionIdInProgress.keys():
            requestPDU = self.completionIdInProgress[pdu.completionId]
            if pdu.ioStatus >> 30 == IOOperationSeverity.STATUS_SEVERITY_ERROR:
                self.mitm_log.warning(f"Received an IO Response with an error IO status: {pdu}\n"
                                      f"For request {requestPDU}")
            if isinstance(requestPDU, DeviceReadRequestPDU):
                self.mitm_log.debug(f"Read response received.")
                self.dealWithReadResponse(pdu, requestPDU)
            elif isinstance(requestPDU, DeviceCreateRequestPDU):
                self.dealWithCreateResponse(pdu, requestPDU)
            elif isinstance(requestPDU, DeviceCloseRequestPDU):
                self.dealWithCloseResponse(pdu, requestPDU)
            else:
                self.mitm_log.debug(f"Unknown response received: {pdu}")
            self.completionIdInProgress.pop(pdu.completionId)
        else:
            self.mitm_log.error(f"Completion id {pdu.completionId} not in the completionId in progress list. "
                                f"This might mean that someone is sending corrupted data.")

    def dealWithReadResponse(self, pdu: DeviceIOResponsePDU, requestPDU: DeviceReadRequestPDU):
        """
        Put data in a BytesIO for later saving.
        """
        readDataResponsePDU = self.deviceRedirectionParser.parseReadResponse(pdu)
        self.pduToSend = readDataResponsePDU
        fileName = self.bytesToPath(self.openedFiles[requestPDU.fileId])
        if fileName not in self.finalFiles.keys():
            self.finalFiles[fileName] = BytesIO()
        stream = self.finalFiles[fileName]
        stream.seek(requestPDU.offset)
        stream.write(readDataResponsePDU.readData)

    def dealWithCreateResponse(self, pdu: DeviceIOResponsePDU, requestPDU: DeviceCreateRequestPDU):
        """
        If its been created for reading, add the file to the list of opened files.
        """
        createResponse = self.deviceRedirectionParser.parseDeviceCreateResponse(pdu)
        self.pduToSend = createResponse
        if requestPDU.desiredAccess & (FileAccess.GENERIC_READ | FileAccess.FILE_READ_DATA) and \
           requestPDU.createOptions & CreateOption.FILE_NON_DIRECTORY_FILE != 0:
            self.mitm_log.info(f"Opening file {requestPDU.path.decode('utf-16')} as number {createResponse.fileId}")
            self.openedFiles[createResponse.fileId] = requestPDU.path

    def dealWithCloseResponse(self, pdu: DeviceIOResponsePDU, requestPDU: DeviceCloseRequestPDU):
        """
        Clean everything and write the file to disk.
        """
        if requestPDU.fileId in self.openedFiles.keys():
            self.mitm_log.info(f"Closing file {requestPDU.fileId}.")
            path = self.bytesToPath(self.openedFiles[requestPDU.fileId])
            self.writeToDisk(path, self.finalFiles[path])
            self.openedFiles.pop(requestPDU.fileId)

    def sendPDU(self, pdu: DeviceRedirectionPDU):
        """
        Write and send the PDU to the upper layers
        """
        data = self.deviceRedirectionParser.write(pdu)
        self.layer.send(data)

    def writeToDisk(self, path: str, stream: BytesIO):
        """
        Sanitize the path, make sure the folders exist and save the provided data on disk.
        """
        goodPath = "./saved_files/" + path.replace("\\", "/").replace("..", "")
        os.makedirs(os.path.dirname(goodPath), exist_ok=True)
        self.mitm_log.info(f"Writing {goodPath} to disk.")
        with open(goodPath, "wb") as file:
            file.write(stream.getvalue())

    def bytesToPath(self, pathAsBytes: bytes):
        """
        Converts a windows-encoded path to a beautiful, python-ready path.
        """
        return pathAsBytes.decode("utf-16le", errors="ignore").replace("\00", "")


class ClientPassiveDeviceRedirectionObserver(PassiveDeviceRedirectionObserver):

    def __init__(self, layer: Layer, recorder: Recorder, **kwargs):
        super().__init__(layer, recorder, ParserMode.CLIENT, **kwargs)


class ServerPassiveDeviceRedirectionObserver(PassiveDeviceRedirectionObserver):

    def __init__(self, layer: Layer, recorder: Recorder, clientObserver: ClientPassiveDeviceRedirectionObserver, **kwargs):
        super().__init__(layer, recorder, ParserMode.SERVER, **kwargs)
        self.clientObserver = clientObserver

    def sendPDU(self, pdu: DeviceRedirectionPDU):
        super(ServerPassiveDeviceRedirectionObserver, self).sendPDU(pdu)
