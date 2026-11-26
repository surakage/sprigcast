// SprigCast's phone sound helper: Android Screen's sound over the USB cable.
//
// SprigCast copies this (a few kilobytes) to the phone's temporary folder over adb and runs it with Android's
// own app_process, as the USB debugging shell: nothing is installed, and it ends when SprigCast stops reading or
// the cable comes out. It records what the phone plays (Android 11 or newer) and writes it to its output as
// 48 kHz stereo 16-bit PCM, which SprigCast reads (sources/android_screen.py). While it runs, the phone plays its
// sound through SprigCast instead of its own speaker (Android sends its whole output to the recording).
//
// Errors go to its error output as one line starting "sprigcast-sound:", and it ends with:
//   2  this Android is older than 11      3  the phone refused to record      1  anything else
package com.slothbloom.sprigcast;

import android.app.Application;
import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.pm.ApplicationInfo;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.Looper;

import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;

public final class Sound {
    static final String SHELL = "com.android.shell";  // the USB debugging shell's own package
    static final int SHELL_UID = 2000;
    static final int RATE = 48000;
    static final int REMOTE_SUBMIX = 8;  // MediaRecorder.AudioSource.REMOTE_SUBMIX: everything the phone plays

    private Sound() {}

    public static void main(String[] args) {
        if (Build.VERSION.SDK_INT < 30) {
            fail(2, "needs Android 11 or newer (this phone has API " + Build.VERSION.SDK_INT + ")");
        }
        Context context = null;
        try {
            context = prepare();
        } catch (Throwable e) {
            note("couldn't prepare (" + e + "), trying anyway");
        }
        AudioRecord record;
        try {
            record = open(context);
        } catch (Throwable e) {
            fail(3, "the phone refused to record its sound: " + e);
            return;
        }
        if (record.getState() != AudioRecord.STATE_INITIALIZED) {
            fail(3, "the phone refused to record its sound (not initialized)");
        }
        try {
            record.startRecording();
        } catch (Throwable e) {
            fail(3, "the phone refused to start recording its sound: " + e);
        }
        byte[] buffer = new byte[RATE / 100 * 4 * 2];  // 20 ms
        OutputStream out = new FileOutputStream(FileDescriptor.out);
        try {
            while (true) {
                int n = record.read(buffer, 0, buffer.length);
                if (n < 0) {
                    fail(1, "reading the sound failed (" + n + ")");
                }
                if (n > 0) {
                    out.write(buffer, 0, n);
                    out.flush();
                }
            }
        } catch (IOException e) {
            // SprigCast stopped reading: done
        } finally {
            try {
                record.stop();
            } catch (Throwable ignored) {
            }
            record.release();
        }
        System.exit(0);
    }

    static AudioRecord open(Context context) throws Exception {
        AudioFormat format = new AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(RATE)
                .setChannelMask(AudioFormat.CHANNEL_IN_STEREO)
                .build();
        int least = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord.Builder builder = new AudioRecord.Builder()
                .setAudioSource(REMOTE_SUBMIX)
                .setAudioFormat(format)
                .setBufferSizeInBytes(Math.max(least, RATE / 10 * 4) * 2);
        if (Build.VERSION.SDK_INT >= 31 && context != null) {
            builder.setContext(context);  // recorded as the shell, which may record the phone's sound
        }
        return builder.build();
    }

    /** What a process that isn't an app needs before Android lets it record: a main thread, an ActivityThread, and
     * a context that says it's the shell. Hidden parts are reached by name; each step is best effort. */
    static Context prepare() throws Exception {
        if (Looper.getMainLooper() == null) {
            Looper.prepareMainLooper();
        }
        Class<?> threadClass = Class.forName("android.app.ActivityThread");
        Constructor<?> make = threadClass.getDeclaredConstructor();
        make.setAccessible(true);
        Object thread = make.newInstance();
        set(threadClass, null, "sCurrentActivityThread", thread);
        try {
            Field system = threadClass.getDeclaredField("mSystemThread");
            system.setAccessible(true);
            system.setBoolean(thread, true);
        } catch (Throwable ignored) {
        }
        Method systemContext = threadClass.getDeclaredMethod("getSystemContext");
        systemContext.setAccessible(true);
        Context base = (Context) systemContext.invoke(thread);
        ShellContext shell = new ShellContext(base);
        try {  // the app this thread is bound to: the shell
            Class<?> bindClass = Class.forName("android.app.ActivityThread$AppBindData");
            Constructor<?> bindMake = bindClass.getDeclaredConstructor();
            bindMake.setAccessible(true);
            Object bind = bindMake.newInstance();
            ApplicationInfo info = new ApplicationInfo();
            info.packageName = SHELL;
            set(bindClass, bind, "appInfo", info);
            set(threadClass, thread, "mBoundApplication", bind);
        } catch (Throwable e) {
            note("no bound app (" + e + ")");
        }
        try {  // and its application
            Application app = new Application();
            Method attach = ContextWrapper.class.getDeclaredMethod("attachBaseContext", Context.class);
            attach.setAccessible(true);
            attach.invoke(app, shell);
            set(threadClass, thread, "mInitialApplication", app);
        } catch (Throwable e) {
            note("no application (" + e + ")");
        }
        return shell;
    }

    static void set(Class<?> owner, Object target, String name, Object value) throws Exception {
        Field field = owner.getDeclaredField(name);
        field.setAccessible(true);
        field.set(target, value);
    }

    static void note(String text) {
        System.err.println("sprigcast-sound: " + text);
    }

    static void fail(int code, String text) {
        note(text);
        System.exit(code);
    }

    /** A context that names the shell, so Android checks the shell's right to record the phone's sound. */
    static final class ShellContext extends ContextWrapper {
        ShellContext(Context base) {
            super(base);
        }

        @Override
        public String getPackageName() {
            return SHELL;
        }

        @Override
        public String getOpPackageName() {
            return SHELL;
        }

        @Override
        public AttributionSource getAttributionSource() {
            return new AttributionSource.Builder(SHELL_UID).setPackageName(SHELL).build();
        }

        @Override
        public Context getApplicationContext() {
            return this;
        }
    }
}
