// The client of the independent peer (IMPLEMENTATION.md §13; task X3). It is a small program of this
// project that drives the OpenMUC j60870 library through its public API, as a black box: it connects
// to a station, starts the data transfer, sends one general interrogation, prints each point it
// receives, and exits when the interrogation is terminated. It keeps no protocol logic of its own.
//
// Usage: java -cp classes:j60870.jar Client --host 172.20.0.5 --port 2404 --common-address 1
// Exit status 0: the interrogation ended. Any other status: it did not, and the reason is printed.

import java.io.IOException;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.openmuc.j60870.ASdu;
import org.openmuc.j60870.ASduType;
import org.openmuc.j60870.CauseOfTransmission;
import org.openmuc.j60870.ClientConnectionBuilder;
import org.openmuc.j60870.Connection;
import org.openmuc.j60870.ConnectionEventListener;
import org.openmuc.j60870.ie.IeQualifierOfInterrogation;
import org.openmuc.j60870.ie.IeShortFloat;
import org.openmuc.j60870.ie.InformationObject;

public final class Client {

    /** The station interrogation (QOI 20, IEC 60870-5-104 §7.5). */
    static final int STATION_INTERROGATION = 20;
    static final long PATIENCE_SECONDS = 30;

    private Client() {}

    public static void main(String[] args) throws Exception {
        String host = null;
        int port = 2404;
        int commonAddress = 1;
        for (int i = 0; i + 1 < args.length; i += 2) {
            switch (args[i]) {
                case "--host" -> host = args[i + 1];
                case "--port" -> port = Integer.parseInt(args[i + 1]);
                case "--common-address" -> commonAddress = Integer.parseInt(args[i + 1]);
                default -> throw new IllegalArgumentException("unknown option " + args[i]);
            }
        }
        if (host == null) {
            System.err.println("the station address is required: --host");
            System.exit(2);
        }
        CountDownLatch transferStarted = new CountDownLatch(1);
        CountDownLatch ended = new CountDownLatch(1);
        ConnectionEventListener listener = new ConnectionEventListener() {
            @Override
            public void newASdu(Connection connection, ASdu asdu) {
                if (asdu.getTypeIdentification() == ASduType.M_ME_NC_1) {
                    for (InformationObject object : asdu.getInformationObjects()) {
                        IeShortFloat value = (IeShortFloat) object.getInformationElements()[0][0];
                        System.out.println(
                                "point " + object.getInformationObjectAddress()
                                        + " value " + value.getValue()
                                        + " cause " + asdu.getCauseOfTransmission());
                    }
                } else if (asdu.getTypeIdentification() == ASduType.C_IC_NA_1
                        && asdu.getCauseOfTransmission() == CauseOfTransmission.ACTIVATION_TERMINATION) {
                    System.out.println("interrogation ended");
                    ended.countDown();
                }
            }

            @Override
            public void dataTransferStateChanged(Connection connection, boolean stopped) {
                if (!stopped) {
                    transferStarted.countDown();
                }
            }

            @Override
            public void connectionClosed(Connection connection, IOException cause) {
                System.out.println("connection closed: " + cause.getMessage());
            }
        };
        Connection connection = new ClientConnectionBuilder(host)
                .setPort(port)
                .setConnectionEventListener(listener)
                .build();
        try {
            connection.startDataTransfer();
            if (!transferStarted.await(PATIENCE_SECONDS, TimeUnit.SECONDS)) {
                System.err.println("the data transfer did not start");
                System.exit(1);
            }
            connection.interrogation(
                    commonAddress,
                    CauseOfTransmission.ACTIVATION,
                    new IeQualifierOfInterrogation(STATION_INTERROGATION));
            if (!ended.await(PATIENCE_SECONDS, TimeUnit.SECONDS)) {
                System.err.println("the interrogation did not end");
                System.exit(1);
            }
        } finally {
            connection.close();
        }
        System.exit(0);
    }
}
