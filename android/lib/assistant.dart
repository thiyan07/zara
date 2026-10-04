// Assistant-role compatibility model (Stage 10 diagnostic spike).
//
// Pure Dart: parses the native getAssistantStatus report. All fields
// default to "not supported/unknown" — absence of data is never success.
class AssistantStatus {
  final bool serviceRegistered;
  final String serviceComponent;
  final String currentAssistant;
  final bool zaraIsDefault;
  final bool roleAvailable;
  final bool roleHeld;
  final bool roleRequestPossible;

  const AssistantStatus({
    this.serviceRegistered = false,
    this.serviceComponent = '',
    this.currentAssistant = '',
    this.zaraIsDefault = false,
    this.roleAvailable = false,
    this.roleHeld = false,
    this.roleRequestPossible = false,
  });

  static AssistantStatus fromMap(Map<String, dynamic> m) => AssistantStatus(
        serviceRegistered: m['service_registered'] == true,
        serviceComponent: '${m['service_component'] ?? ''}',
        currentAssistant: '${m['current_assistant'] ?? ''}',
        zaraIsDefault: m['zara_is_default'] == true,
        roleAvailable: m['role_available'] == true,
        roleHeld: m['role_held'] == true,
        roleRequestPossible: m['role_request_possible'] == true,
      );

  String describe() {
    if (!serviceRegistered) return 'Assistant service: not registered';
    if (zaraIsDefault) return 'Zara IS the default assistant';
    final cur = currentAssistant.isEmpty ? 'none' : currentAssistant;
    return 'Assistant service registered; default: $cur';
  }
}
