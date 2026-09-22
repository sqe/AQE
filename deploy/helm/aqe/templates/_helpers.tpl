{{- define "aqe.labels" -}}
app.kubernetes.io/part-of: aqe
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "aqe.componentLabels" -}}
app.kubernetes.io/name: aqe-{{ .component }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{ include "aqe.labels" .root }}
{{- end }}

{{- define "aqe.env" -}}
- {name: OBJECT_STORE_ENDPOINT, value: {{ .Values.config.objectStoreEndpoint | quote }}}
- {name: QDRANT_HOST, value: {{ .Values.config.qdrantHost | quote }}}
- {name: QDRANT_HTTPS, value: {{ .Values.config.qdrantHttps | quote }}}
- {name: REDIS_HOST, value: {{ .Values.config.redisHost | quote }}}
- {name: KAFKA_BROKER, value: {{ .Values.config.kafkaBootstrapServers | quote }}}
- {name: KAFKA_BOOTSTRAP_SERVERS, value: {{ .Values.config.kafkaBootstrapServers | quote }}}
{{- end }}
