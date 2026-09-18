"""Shutdown must stop USB I/O before SET_MODE, and must not raise on teardown USB errors."""

from unittest.mock import MagicMock

import can
import usb1

from usbtingobus import USBtingoBus, USBtingoUSBEventHandler


def _transfer(*, submitted=True):
    transfer = MagicMock()
    transfer.isSubmitted.return_value = submitted
    transfer.inuse = submitted
    return transfer


def _bus():
    bus = USBtingoBus.__new__(USBtingoBus)
    bus.running = True
    bus.recordingActive = False
    bus.ep1in_transfer = [_transfer()]
    bus.ep2in_transfer = [_transfer(submitted=False)]
    bus.ep3in_transfer = [_transfer(), _transfer()]
    bus.ep3out_transfer = [_transfer()]
    bus.usbhandle = MagicMock()
    bus.usbdev = MagicMock()
    bus.ctx = MagicMock()
    bus.eventthread = USBtingoUSBEventHandler(bus)
    bus.eventthread.join = MagicMock()
    return bus


def test_rx_callback_does_not_resubmit_after_stop():
    bus = _bus()
    bus.running = False
    transfer = MagicMock()
    transfer.getBuffer.return_value = b""
    transfer.getActualLength.return_value = 0

    assert bus.usbtransfer_ep3in_callback(transfer) is False


def test_status_callback_does_not_resubmit_after_stop():
    bus = _bus()
    bus.running = False
    bus.statusreport_listeners = []
    transfer = MagicMock()
    transfer.getBuffer.return_value = bytearray(32)

    assert bus.usbtransfer_ep1in_callback(transfer) is False


def test_tx_callback_does_not_resubmit_after_stop():
    bus = _bus()
    bus.running = False
    bus.tx_queue = __import__("queue").Queue()
    bus.tx_queue.put_nowait(can.Message(arbitration_id=0x123, data=[1, 2, 3]))
    transfer = MagicMock()
    transfer.inuse = True

    assert bus.usbtransfer_ep3out_callback(transfer) is False
    transfer.setBuffer.assert_not_called()


def test_shutdown_cancels_in_flight_transfers_then_joins_before_set_mode():
    bus = _bus()
    order = []

    for transfer in (*bus.ep1in_transfer, *bus.ep3in_transfer, *bus.ep3out_transfer):
        transfer.cancel.side_effect = lambda _t=transfer: order.append("cancel")
    bus.ctx.interruptEventHandler.side_effect = lambda: order.append("interrupt")
    bus.eventthread.join.side_effect = lambda *args, **kwargs: order.append("join")
    bus.usbhandle.controlWrite.side_effect = lambda *args, **kwargs: order.append("set_mode")

    bus.shutdown()

    assert order[:4] == ["cancel", "cancel", "cancel", "cancel"]
    assert order[4:] == ["interrupt", "join", "set_mode"]
    bus.ep2in_transfer[0].cancel.assert_not_called()
    bus.usbdev.close.assert_called_once()
    bus.ctx.close.assert_called_once()


def test_shutdown_swallows_set_mode_usb_error():
    bus = _bus()
    bus.usbhandle.controlWrite.side_effect = usb1.USBErrorIO()

    bus.shutdown()

    bus.usbdev.close.assert_called_once()
    bus.ctx.close.assert_called_once()


def test_event_thread_stop_interrupts_libusb_event_loop():
    bus = _bus()
    thread = USBtingoUSBEventHandler(bus)

    thread.stop()

    assert thread.running is False
    bus.ctx.interruptEventHandler.assert_called_once()


def test_shutdown_stops_callbacks_before_recording_stop():
    bus = _bus()
    seen = {}

    def recording_stop():
        seen["running"] = bus.running

    bus.recording_stop = recording_stop

    bus.shutdown()

    assert seen["running"] is False


def test_recording_stop_does_not_wait_forever_for_last_packet(monkeypatch):
    bus = _bus()
    bus.recordingActive = True
    bus.logicoutfile = MagicMock()
    bus.logicoutfilename = "capture.sr"
    bus.samplingrate = 1000
    bus.usbhandle.controlRead.return_value = b"\x00\x00\x00\x00"
    waits = []

    class FakeEvent:
        def wait(self, timeout=None):
            waits.append(timeout)
            return False

    monkeypatch.setattr("usbtingobus.threading.Event", FakeEvent)
    monkeypatch.setattr("usbtingobus.zipfile.ZipFile", MagicMock())

    bus.recording_stop()

    assert waits == [2.0]


def test_shutdown_skips_usb_teardown_if_event_thread_stays_alive():
    bus = _bus()
    bus.eventthread.is_alive = MagicMock(return_value=True)

    bus.shutdown()

    bus.usbhandle.controlWrite.assert_not_called()
    bus.usbdev.close.assert_not_called()
    bus.ctx.close.assert_not_called()
