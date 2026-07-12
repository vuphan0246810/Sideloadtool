package com.superalpha.sideload.bridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import com.superalpha.sideload.MainActivity
import com.superalpha.sideload.R

/**
 * Minimal foreground service whose only job is to keep the process alive (with an
 * ongoing notification, as Android requires) while a sideload/revoke/install
 * operation that depends on the raw USB connection is in progress. It does not itself
 * touch USB — [UsbTransport] holds the actual connection — this class exists purely
 * to satisfy Android's background-execution limits during a potentially multi-minute
 * IPA install.
 */
class UsbBridgeService : Service() {
    private val channelId = "usb_bridge_channel"

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildNotification())
        return START_STICKY
    }

    private fun buildNotification(): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = getSystemService(NotificationManager::class.java)
            val channel = NotificationChannel(
                channelId,
                getString(R.string.usb_service_channel_name),
                NotificationManager.IMPORTANCE_LOW
            )
            manager.createNotificationChannel(channel)
        }

        val openAppIntent = Intent(this, MainActivity::class.java)
        val pendingIntent = android.app.PendingIntent.getActivity(
            this, 0, openAppIntent,
            android.app.PendingIntent.FLAG_IMMUTABLE
        )

        return Notification.Builder(this, channelId)
            .setContentTitle(getString(R.string.usb_service_notification_title))
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val NOTIFICATION_ID = 42
    }
}
