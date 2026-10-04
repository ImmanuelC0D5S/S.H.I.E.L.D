import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.ksp)
    alias(libs.plugins.google.services)
}

/**
 * Supabase endpoint and publishable key, read at build time from
 * `local.properties` (gitignored) or the environment.
 *
 * Why build time and not runtime: this app has no backend of its own, so these
 * two values have to be compiled in. Neither is a secret - the publishable key
 * ships inside every APK that ever exists and is only meaningful alongside RLS -
 * but the *service* key must never reach this module, and having no code path
 * that could read it is stronger than a review rule.
 *
 * `SUPABASE_SERVICE_KEY` is deliberately absent. If you find yourself adding it
 * here, the RPi's role separation (see shield-rpi alerts.py) has been undone.
 */
val shieldConfig: Properties = Properties().apply {
    val local = rootProject.file("local.properties")
    if (local.exists()) {
        local.inputStream().use { load(it) }
    }
}

fun shieldProperty(key: String, env: String): String? =
    (shieldConfig.getProperty(key) ?: System.getenv(env))?.trim()?.takeIf { it.isNotEmpty() }

val supabaseUrl = shieldProperty("SHIELD_SUPABASE_URL", "SHIELD_SUPABASE_URL")
val supabaseAnonKey = shieldProperty("SHIELD_SUPABASE_ANON_KEY", "SHIELD_SUPABASE_ANON_KEY")

android {
    namespace = "com.immanuelc0d5s.shield"
    // Must match package_name in google-services.json exactly. FCM registration
    // tokens are bound to the package name; a mismatch produces an app that
    // registers successfully and then never receives a message.
    compileSdk = libs.versions.compileSdk.get().toInt()

    defaultConfig {
        applicationId = "com.immanuelc0d5s.shield"
        minSdk = libs.versions.minSdk.get().toInt()
        targetSdk = libs.versions.targetSdk.get().toInt()
        versionCode = 1
        versionName = "0.1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        buildConfigField("String", "SUPABASE_URL", "\"${supabaseUrl ?: ""}\"")
        buildConfigField("String", "SUPABASE_ANON_KEY", "\"${supabaseAnonKey ?: ""}\"")
    }

buildTypes {
        debug {
            // Deliberately NO applicationIdSuffix. FCM registration tokens are
            // bound to the package name registered in Firebase, so a `.debug`
            // suffix produces a package that google-services.json has no client
            // for: the build fails outright, and even if a client were added, the
            // debug build would receive pushes into a different app than the
            // release one. A debug variant that cannot receive alerts is worse
            // than one that shares the real id.
            //
            // Use the version name to tell builds apart, and `adb shell pm list
            // packages` if two are ever installed at once.
            versionNameSuffix = "-debug"
        }
        release {
            // The project has no release keystore and Phase 9 owns signing. Left
            // on the debug signing config deliberately so `assembleRelease`
            // produces an installable artifact rather than failing on a missing
            // keystore that nobody has been told to create.
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
        // SQLCipher and Firebase both ship Java 8+ bytecode; without this the
        // desugaring step fails on their java.time usage.
        isCoreLibraryDesugaringEnabled = false
    }

    kotlin {
        compilerOptions {
            jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
        }
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    packaging {
        resources {
            excludes += setOf(
                "/META-INF/{AL2.0,LGPL2.1}",
                "/META-INF/DEPENDENCIES",
                "/META-INF/LICENSE*",
            )
        }
    }
}

ksp {
    // Room writes its schemas here so migrations can be diffed in review. Schema
    // history is committed; the generated code is not.
    arg("room.schemaLocation", "$projectDir/schemas")
    arg("room.incremental", "true")
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.process)

    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.material.icons)
    debugImplementation(libs.androidx.compose.ui.tooling)

    implementation(libs.androidx.navigation.compose)

    implementation(libs.androidx.room.runtime)
    implementation(libs.androidx.room.ktx)
    ksp(libs.androidx.room.compiler)
    implementation(libs.androidx.sqlite.ktx)
    // P0 #4. Replaces the platform SQLite on this process only.
    implementation(libs.sqlcipher.android)

    implementation(platform(libs.firebase.bom))
    implementation(libs.firebase.messaging)

    // Snapshot thumbnails are signed Supabase Storage URLs. Coil is the only
    // non-Android dependency here and earns its place: hand-rolling a
    // downsampling, disk-caching image loader for a list of photos is exactly
    // the kind of code that is written badly once and never revisited.
    implementation(libs.coil.compose)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    androidTestImplementation(libs.androidx.junit)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(platform(libs.androidx.compose.bom))
    androidTestImplementation(libs.androidx.compose.ui.test.junit4)
    debugImplementation(libs.androidx.compose.ui.test.manifest)
}