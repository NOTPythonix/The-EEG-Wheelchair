#include &lt;WiFi.h&gt;
#include &lt;USB.h&gt;
#include &lt;USBHostSerial.h&gt;
// ---------- Wi-Fi settings: edit these ----------
const char* WIFI_SSID = &quot;YOUR_WIFI_NAME&quot;;
const char* WIFI_PASSWORD = &quot;YOUR_WIFI_PASSWORD&quot;;

Knight Board Wireless Bridge • Setup and Troubleshooting

// Replace with the Jetson&#39;s LAN IP address.
const char* JETSON_IP = &quot;192.168.1.100&quot;;
const uint16_t JETSON_PORT = 8765;
// USB CDC device connected to the ESP32-S3 host port.
USBHostSerialClass EEGSerial;
WiFiClient client;
uint8_t transferBuffer[1024];
void connectWiFi() {
WiFi.mode(WIFI_STA);
WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
Serial.print(&quot;Connecting to Wi-Fi&quot;);
while (WiFi.status() != WL_CONNECTED) {
delay(500);
Serial.print(&quot;.&quot;);
}
Serial.println();
Serial.print(&quot;ESP32 IP: &quot;);
Serial.println(WiFi.localIP());
}
bool connectJetson() {
if (client.connected()) return true;
client.stop();
Serial.println(&quot;Connecting to Jetson...&quot;);
if (client.connect(JETSON_IP, JETSON_PORT)) {
client.setNoDelay(true);
Serial.println(&quot;Jetson connected.&quot;);
return true;
}
Serial.println(&quot;Jetson connection failed.&quot;);
return false;
}
void setup() {
Serial.begin(115200);
delay(1000);
Serial.println(&quot;\\nKnight Board wireless bridge&quot;);
connectWiFi();
if (!USBHost.begin()) {
Serial.println(&quot;USB host initialization failed!&quot;);
while (true) delay(1000);
}
EEGSerial.begin(115200);
Serial.println(&quot;USB host started.&quot;);
Serial.println(&quot;Connect the Knight Board to the USB-host connector.&quot;);
}
void loop() {
USBHost.task();

Knight Board Wireless Bridge • Setup and Troubleshooting

if (WiFi.status() != WL_CONNECTED) {
client.stop();
delay(100);
return;
}
if (!client.connected()) {
if (!connectJetson()) {
delay(500);
return;
}
}
if (!EEGSerial.mounted()) {
delay(1);
return;
}
int availableBytes = EEGSerial.available();
if (availableBytes &lt;= 0) {
delay(1);
return;
}
size_t amount = (size_t)availableBytes;
if (amount &gt; sizeof(transferBuffer)) amount = sizeof(transferBuffer);
size_t received = EEGSerial.readBytes(transferBuffer, amount);
if (received &gt; 0) {
size_t sent = client.write(transferBuffer, received);
if (sent != received) {
Serial.println(&quot;WARNING: TCP write incomplete; reconnecting.&quot;);
client.stop();
}
}
}
