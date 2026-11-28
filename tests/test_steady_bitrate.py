"""Settings, Stream, Quality, Fine tuning: Steady bitrate (pad still pictures up to the bitrate, or don't)."""

from sprigcast.core.scene import EncoderSettings
from sprigcast.output.recorder import video_options


def test_on_by_default_keeps_the_padding_each_encoder_had():
    enc = EncoderSettings()
    assert enc.steady_bitrate
    assert video_options("h264_nvenc", enc, 4500)["cbr_padding"] == "1"
    assert video_options("h264_amf", enc, 4500)["filler_data"] == "1"
    assert video_options("libx264", enc, 4500)["nal-hrd"] == "cbr"
    assert "nal-hrd" not in video_options("libx264", enc, 4500, hrd=False)  # auto bitrate needs it out


def test_off_sends_only_what_the_picture_needs():
    enc = EncoderSettings(steady_bitrate=False)
    nvenc = video_options("h264_nvenc", enc, 4500)
    assert "cbr_padding" not in nvenc
    assert (nvenc["rc"], nvenc["maxrate"]) == ("cbr", "4500k")  # still capped at the bitrate
    amf = video_options("h264_amf", enc, 4500)
    assert amf["filler_data"] == "0" and amf["enforce_hrd"] == "1"
    x264 = video_options("libx264", enc, 4500)
    assert "nal-hrd" not in x264 and x264["maxrate"] == "4500k"


def test_old_saved_settings_load_with_it_on():
    assert EncoderSettings.model_validate({"encoder": "h264_nvenc", "rate_control": "cbr"}).steady_bitrate


def test_the_switch_shows_only_where_it_does_something():
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.output_page import EncoderForm

    QApplication.instance() or QApplication([])
    form = EncoderForm("Video", EncoderSettings(encoder="libx264", steady_bitrate=False), "libx264")
    form.set_expert(True)
    assert not form.steady_bitrate.isChecked()
    assert not form.steady_bitrate.isHidden()
    assert not form.values().steady_bitrate
    form.rate_control.setCurrentIndex(form.rate_control.findData("vbr"))
    assert form.steady_bitrate.isHidden()
    form.rate_control.setCurrentIndex(form.rate_control.findData("cbr"))
    form.reset_to_defaults()
    assert form.values().steady_bitrate
