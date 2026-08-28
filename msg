Pour clarifier, il ne s’agit pas ici d’une limitation liée à notre développement. Le comportement dépend directement de la solution IBM Cloud Object Storage et des fonctionnalités exposées par IBM dans sa console.

IBM étant l’éditeur et l’architecte de la solution COS, notre rôle est de nous appuyer sur les mécanismes qu’il fournit et supporte. Pour le téléchargement de fichiers volumineux, IBM prévoit notamment l’utilisation de la CLI ou de l’API.


La raison pour laquelle IBM ne propose pas forcément le téléchargement de très gros fichiers directement dans l’interface graphique est plutôt une décision d’architecture/produit. Un téléchargement de 5, 20 ou 100 Go dans un navigateur est plus fragile : perte de connexion, expiration de session/authentification, proxy d’entreprise, reprise après interruption, parallélisation, etc. 