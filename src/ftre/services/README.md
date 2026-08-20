# services

Owner: shared, stateful runtime capabilities.  Each service has one Provider
Plugin, a stable service key and a lifecycle-bound disposer.  Feature code may
consume these keys but must not import a provider's private runtime modules.

