# RPi alerts: the app talks to Supabase over https. Nothing here is obfuscated
# away that matters, but the API base URL and anon key are BuildConfig constants
# and there is no reason to let a decompiler see the string table.
-keepattributes Signature
-keepattributes *Annotation*
-keepattributes SourceFile,LineNumberTable

# Room generates these; R8 cannot see the reflection behind them.
-keep class * extends androidx.room.RoomDatabase
-dontwarn androidx.room.paging.**

# SQLCipher loads its native library via JNI and is loaded reflectively by
# SupportFactory. R8 has no way to see either, so it strips the provider classes
# and the app dies at first database open with a ClassNotFoundException.
-keep class net.zetetic.database.sqlcipher.** { *; }
-keep class net.sqlcipher.** { *; }
-keep class androidx.sqlite.** { *; }
-dontwarn net.zetetic.**
-dontwarn net.sqlcipher.**

# Firebase Messaging reads its defaults from a generated string array and
# instantiates its transport reflectively.
-keep class com.google.firebase.** { *; }
-keep class com.google.android.gms.** { *; }
-dontwarn com.google.firebase.**

# Kotlin coroutines' internal atomics. R8 warns loudly and strips the classes.
-dontwarn kotlinx.coroutines.**
-keepclassmembers class kotlinx.coroutines.** { volatile <fields>; }

# OkHttp is pulled in transitively by Firebase; these are its optional
# dependencies and their absence is not an error at runtime.
-dontwarn okhttp3.**
-dontwarn okio.**
-dontwarn javax.annotation.**

# Keep the Compose runtime's own metadata; R8 cannot always prove it is used.
-keep class androidx.compose.runtime.** { *; }