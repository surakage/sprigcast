// SprigCast for Streamer.bot: paste into an "Execute C# Code" sub-action, name the action "SprigCast",
// press Compile, then Save. Other actions call it with Core → C# → "Execute C# Method":
//   action "SprigCast", method e.g. "ShowBloom", after setting the arguments it needs (bloom, sound...).
//
// Once: in Streamer.bot set two global variables (Variables → Global Variables, Persisted):
//   sprigcastPort      the port in SprigCast Settings → Connections (4455, or 4456 next to OBS)
//   sprigcastPassword  the key from Settings → Connections → Pair a device (or the password shown there)
// and in SprigCast turn on the API and "Allow web links".
//
// Every method sets %sprigcastResult% (SprigCast's JSON answer) and %sprigcastOk% (True/False).
// See docs/streamerbot.md and docs/control-api.md for every action.

using System;
using System.Collections.Generic;
using System.Net;
using System.Text;

public class CPHInline
{
    // Any action: set %sprigcastAction% (e.g. "bloom.show") and %sprigcastJson% (e.g. {"bloom": "BRB"}).
    public bool Execute()
    {
        string action, json;
        if (!CPH.TryGetArg("sprigcastAction", out action) || string.IsNullOrEmpty(action))
        {
            CPH.LogWarn("SprigCast: set the sprigcastAction argument, e.g. bloom.show");
            return false;
        }
        if (!CPH.TryGetArg("sprigcastJson", out json) || string.IsNullOrEmpty(json))
            json = "{}";
        return Call(action, json);
    }

    // %bloom%, optional %shape% (horizontal or vertical)
    public bool ShowBloom() { return Call("bloom.show", Fields("bloom", "shape")); }

    // %bloom% and %item%
    public bool ShowItem() { return Call("item.show", Fields("bloom", "item")); }
    public bool HideItem() { return Call("item.hide", Fields("bloom", "item")); }
    public bool ToggleItem() { return Call("item.toggle", Fields("bloom", "item")); }

    // %sprig% and %state%
    public bool SprigState() { return Call("sprig.state", Fields("sprig", "state")); }

    // %sound%
    public bool MuteSound() { return Call("sound.mute", With(Fields("sound"), "\"muted\": true")); }
    public bool UnmuteSound() { return Call("sound.mute", With(Fields("sound"), "\"muted\": false")); }
    public bool ToggleSound() { return Call("sound.mute", Fields("sound")); }

    // %source% and %text%: change what a text source says
    public bool SetText()
    {
        string source, text;
        CPH.TryGetArg("source", out source);
        CPH.TryGetArg("text", out text);
        return Call("source.set", "{\"source\": " + Quote(source) + ", \"settings\": {\"text\": " + Quote(text) + "}}");
    }

    // %source% and %filter%
    public bool FilterOn() { return Call("filter.set", With(Fields("source", "filter"), "\"on\": true")); }
    public bool FilterOff() { return Call("filter.set", With(Fields("source", "filter"), "\"on\": false")); }
    public bool ToggleFilter() { return Call("filter.toggle", Fields("source", "filter")); }

    // %media%
    public bool PlayMedia() { return Call("media.restart", Fields("media")); }
    public bool StopMedia() { return Call("media.stop", Fields("media")); }

    public bool Screenshot() { return Call("screenshot.take", "{}"); }
    public bool SaveClip() { return Call("clip.save", "{}"); }
    public bool ToggleStream() { return Call("stream.toggle", "{}"); }
    public bool ToggleRecord() { return Call("record.toggle", "{}"); }

    // %garden% / %path%
    public bool SwitchGarden() { return Call("garden.switch", Fields("garden")); }
    public bool SwitchPath() { return Call("path.switch", Fields("path")); }

    // Store plugins (add them first in SprigCast Settings → Plugins)
    public bool DeathAdd() { return Call("deaths.add", "{}"); }
    public bool DeathRemove() { return Call("deaths.remove", "{}"); }
    public bool DeathReset() { return Call("deaths.reset", "{}"); }
    public bool DeathShow() { return Call("deaths.show", "{}"); }
    // %source%, optional %seconds%
    public bool Pixelate() { return Call("hide.pixelate", Fields("source", "seconds")); }
    public bool Blackout() { return Call("hide.blackout", Fields("source", "seconds")); }
    // %sprig%, optional %seconds%
    public bool BarrelRoll() { return Call("roll.spin", Fields("sprig", "seconds")); }

    // ------------------------------------------------------------------------------------------

    private string Fields(params string[] names)
    {
        var parts = new List<string>();
        foreach (var name in names)
        {
            string value;
            if (!CPH.TryGetArg(name, out value) || value == null)
                continue;
            double number;
            bool isNumber = name == "seconds" && double.TryParse(value, System.Globalization.NumberStyles.Float,
                                                                  System.Globalization.CultureInfo.InvariantCulture, out number);
            parts.Add(Quote(name) + ": " + (isNumber ? value : Quote(value)));
        }
        return "{" + string.Join(", ", parts) + "}";
    }

    private static string With(string json, string extra)
    {
        string inside = json.Substring(1, json.Length - 2).Trim();
        return "{" + (inside.Length > 0 ? inside + ", " : "") + extra + "}";
    }

    private static string Quote(string text)
    {
        if (text == null)
            return "null";
        var sb = new StringBuilder("\"");
        foreach (char c in text)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < ' ') sb.Append("\\u" + ((int)c).ToString("x4"));
                    else sb.Append(c);
                    break;
            }
        }
        return sb.Append('"').ToString();
    }

    private bool Call(string action, string json)
    {
        string port = CPH.GetGlobalVar<string>("sprigcastPort", true);
        string password = CPH.GetGlobalVar<string>("sprigcastPassword", true);
        if (string.IsNullOrEmpty(port))
            port = "4455";
        try
        {
            using (var web = new WebClient())
            {
                web.Encoding = Encoding.UTF8;
                web.Headers[HttpRequestHeader.ContentType] = "application/json";
                web.Headers[HttpRequestHeader.Authorization] = "Bearer " + (password ?? "");
                string answer = web.UploadString("http://127.0.0.1:" + port + "/api/" + action, "POST", json);
                CPH.SetArgument("sprigcastResult", answer);
                CPH.SetArgument("sprigcastOk", true);
                return true;
            }
        }
        catch (WebException ex)
        {
            string answer = "";
            if (ex.Response != null)
                using (var reader = new System.IO.StreamReader(ex.Response.GetResponseStream()))
                    answer = reader.ReadToEnd();
            CPH.SetArgument("sprigcastResult", answer);
            CPH.SetArgument("sprigcastOk", false);
            CPH.LogWarn("SprigCast " + action + ": " + (answer != "" ? answer : ex.Message));
            return false;
        }
    }
}
