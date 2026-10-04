package com.immanuelc0d5s.shield

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.History
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material.icons.outlined.Shield
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavDestination.Companion.hierarchy
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.immanuelc0d5s.shield.data.ShieldRepository
import com.immanuelc0d5s.shield.ui.HistoryScreen
import com.immanuelc0d5s.shield.ui.PermissionGate
import com.immanuelc0d5s.shield.ui.SettingsScreen
import com.immanuelc0d5s.shield.ui.theme.ShieldTheme

/**
 * The only non-alert screen: History, Settings, and a permissions tab.
 *
 * Deliberately no login and no onboarding beyond permissions. The app registers a
 * device token and shows events; anything else is friction in front of a screen
 * someone reaches at 3am.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            ShieldTheme {
                MainRoot()
            }
        }
    }
}

private enum class Tab(val route: String, val label: String, val icon: ImageVector) {
    HISTORY("history", "History", Icons.Outlined.History),
    SETTINGS("settings", "Settings", Icons.Outlined.Settings),
    PERMISSIONS("permissions", "Permissions", Icons.Outlined.Shield),
}

@Composable
private fun MainRoot() {
    val context = LocalContext.current
    val viewModel: MainViewModel = viewModel(
        factory = MainViewModel.factory(ShieldRepository.get(context)),
    )
    val state by viewModel.state.collectAsStateWithLifecycle()

    val navController = rememberNavController()
    val backStackEntry by navController.currentBackStackEntryAsState()

    Scaffold(
        bottomBar = {
            NavigationBar {
                Tab.entries.forEach { tab ->
                    val selected =
                        backStackEntry?.destination?.hierarchy?.any { it.route == tab.route } == true
                    NavigationBarItem(
                        selected = selected,
                        onClick = { navController.navigateToTab(tab) },
                        icon = { Icon(tab.icon, contentDescription = tab.label) },
                        label = { Text(tab.label) },
                    )
                }
            }
        },
    ) { padding ->
        NavHost(
            navController = navController,
            startDestination = Tab.HISTORY.route,
            modifier = Modifier
                .fillMaxSize()
                .padding(padding),
        ) {
            composable(Tab.HISTORY.route) {
                HistoryScreen(events = state.events, nowSeconds = state.nowSeconds)
            }

            composable(Tab.SETTINGS.route) {
                SettingsScreen(
                    armed = state.armed,
                    statusMessage = state.statusMessage,
                    localEventCount = state.localEventCount,
                    onArmedChange = viewModel::setArmed,
                    onRefresh = viewModel::refresh,
                )
            }

            composable(Tab.PERMISSIONS.route) {
                Column(modifier = Modifier.fillMaxSize()) {
                    PermissionGate(onAllGranted = viewModel::refresh)
                }
            }
        }
    }
}

private fun androidx.navigation.NavHostController.navigateToTab(tab: Tab) {
    navigate(tab.route) {
        // Preserve each tab's scroll position and state across switches, so
        // returning to History does not jump back to the top of the list.
        popUpTo(graph.startDestinationId) { saveState = true }
        launchSingleTop = true
        restoreState = true
    }
}