C=======================================================================
C     MATSOL   -   LINEAR ALGEBRA KERNELS FOR THE IMPES PRESSURE SOLVE
C
C     WRITTEN   19-JAN-1989   R.T. HALSEY
C     REVISED   07-MAY-1991   LSOR REPLACES POINT JACOBI
C     REVISED   28-FEB-1993   ORTHOMIN(K) ADDED FOR STIFF GAS CASES
C     REVISED   09-SEP-1996   BANDWIDTH NOW TAKEN FROM /GDIM/ NOT NX
C
C     THE MATRIX IS STORED AS SEVEN DIAGONALS IN NATURAL ORDERING:
C         AE  AW  AN  AS  AT  AB  AD
C     WITH AD THE MAIN DIAGONAL.  THIS IS NOT A GENERAL SPARSE SOLVER
C     AND WILL PRODUCE NONSENSE IF THE CONNECTIVITY IS NOT THE SEVEN
C     POINT STENCIL BUILT BY TRNCAL.
C=======================================================================
      SUBROUTINE THOMAS (A, B, C, D, X, N)
C-----------------------------------------------------------------------
C     TRIDIAGONAL SOLVE, NO PIVOTING.  A = SUB, B = DIAG, C = SUPER.
C     D IS DESTROYED.  USED BY THE LINE SOR SWEEPS AND BY WELLBORE.
C-----------------------------------------------------------------------
C     --- nativegate: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- nativegate: end INCLUDE 'PETRO.INC' ---
      PARAMETER (MXBAND = 512)
      DIMENSION A(N), B(N), C(N), D(N), X(N)
      DIMENSION CP(MXBAND), DP(MXBAND)
C
      IF (N .GT. MXBAND) THEN
         IERR = 21
         RETURN
      END IF
      IF (DABS(B(1)) .LT. SMALL) THEN
         IERR = 22
         RETURN
      END IF
C
      CP(1) = C(1) / B(1)
      DP(1) = D(1) / B(1)
      DO 100 I = 2, N
         DEN = B(I) - A(I)*CP(I-1)
         IF (DABS(DEN) .LT. SMALL) THEN
            IERR = 22
            RETURN
         END IF
         CP(I) = C(I) / DEN
         DP(I) = (D(I) - A(I)*DP(I-1)) / DEN
  100 CONTINUE
C
      X(N) = DP(N)
      DO 110 I = N-1, 1, -1
         X(I) = DP(I) - CP(I)*X(I+1)
  110 CONTINUE
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE LSOR (AE, AW, AN, AS, AT, AB, AD, RHS, X,
     &                 OMEGA, TOL, ITMAX, ITUSED, RESID)
C-----------------------------------------------------------------------
C     LINE SUCCESSIVE OVER RELAXATION IN THE X DIRECTION.
C     ONE TRIDIAGONAL SOLVE PER (J,K) LINE, THEN RELAX.
C-----------------------------------------------------------------------
C     --- nativegate: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- nativegate: end INCLUDE 'PETRO.INC' ---
C     --- nativegate: expanded INCLUDE 'GRID.INC' ---
C=======================================================================
C     GRID.INC   -   RESERVOIR GRID AND SOLUTION ARRAYS
C
C     ORIGINALLY WRITTEN  08-JUL-1989   M. OKONKWO / R.T. HALSEY
C     REVISED             22-APR-1992   NX*NY*NZ RAISED TO 40000
C     REVISED             17-OCT-1995   ADDED NON-NEIGHBOUR CONNECTIONS
C
C     STORAGE IS COLUMN-MAJOR NATURAL ORDER:
C         L = I + (J-1)*NX + (K-1)*NX*NY
C     EVERY ROUTINE IN THIS LIBRARY ASSUMES THAT INDEXING.  IF YOU
C     CHANGE IT YOU WILL BREAK TRANX/TRANY/TRANZ AND THE JACOBIAN
C     BANDWIDTH ASSUMPTION IN MATSOL.
C=======================================================================
      PARAMETER (MXCELL = 40000)
      PARAMETER (MXWELL = 200)
      PARAMETER (MXPERF = 50)
      PARAMETER (MXNNC  = 2000)
C
C-----GRID DIMENSIONS ---------------------------------------------------
      COMMON /GDIM/ NX, NY, NZ, NCELL, NACTIV, NNNC
C
C-----STATIC ROCK PROPERTIES -------------------------------------------
      COMMON /ROCK/ PORO(MXCELL), PERMX(MXCELL), PERMY(MXCELL),
     &              PERMZ(MXCELL), DXC(MXCELL), DYC(MXCELL),
     &              DZC(MXCELL), TOPS(MXCELL), NTG(MXCELL)
C
C-----TRANSMISSIBILITIES (BUILT ONCE BY TRNCAL) ------------------------
      COMMON /TRANS/ TRANX(MXCELL), TRANY(MXCELL), TRANZ(MXCELL)
C
C-----PRIMARY UNKNOWNS AND OLD TIME LEVEL ------------------------------
      COMMON /STATE/ PRES(MXCELL), SWAT(MXCELL), SGAS(MXCELL),
     &               POLD(MXCELL), SWOLD(MXCELL), SGOLD(MXCELL)
C
C-----ACTIVE CELL MAP.  IACT(L) = 0 MEANS PINCHED OUT / INACTIVE --------
      COMMON /ACTMAP/ IACT(MXCELL), LNUM(MXCELL)
C
C-----WELL DATA --------------------------------------------------------
C     IWTYPE  1 = PRODUCER (BHP), 2 = PRODUCER (RATE),
C             3 = INJECTOR (BHP), 4 = INJECTOR (RATE)
C
      COMMON /WELLS/ WBHP(MXWELL), WRATE(MXWELL), WWI(MXWELL,MXPERF),
     &               WSKIN(MXWELL), WRAD(MXWELL),
     &               IWCELL(MXWELL,MXPERF), NPERF(MXWELL),
     &               IWTYPE(MXWELL), NWELL
      CHARACTER*8   WNAME
      COMMON /WNAMC/ WNAME(MXWELL)
C
C-----TIME STEPPING ----------------------------------------------------
      COMMON /TSTEP/ TIME, DT, DTMIN, DTMAX, DTFAC, TEND, NSTEP, NCUT
C
C     --- nativegate: end INCLUDE 'GRID.INC' ---
      DIMENSION AE(MXCELL), AW(MXCELL), AN(MXCELL), AS(MXCELL)
      DIMENSION AT(MXCELL), AB(MXCELL), AD(MXCELL)
      DIMENSION RHS(MXCELL), X(MXCELL)
      DIMENSION TA(512), TB(512), TC(512), TD(512), TX(512)
C
      IF (NX .GT. 512) THEN
         IERR = 23
         RETURN
      END IF
C
      R0 = RESNRM(AE, AW, AN, AS, AT, AB, AD, RHS, X)
      IF (R0 .LT. SMALL) R0 = ONE
C
      DO 300 ITER = 1, ITMAX
         DO 220 K = 1, NZ
         DO 210 J = 1, NY
            DO 200 I = 1, NX
               L  = I + (J-1)*NX + (K-1)*NX*NY
               TA(I) = -AW(L)
               TB(I) =  AD(L)
               TC(I) = -AE(L)
               SUM   =  RHS(L)
               IF (J .GT.  1) SUM = SUM + AS(L)*X(L-NX)
               IF (J .LT. NY) SUM = SUM + AN(L)*X(L+NX)
               IF (K .GT.  1) SUM = SUM + AB(L)*X(L-NX*NY)
               IF (K .LT. NZ) SUM = SUM + AT(L)*X(L+NX*NY)
               TD(I) = SUM
  200       CONTINUE
            TA(1)  = ZERO
            TC(NX) = ZERO
            CALL THOMAS (TA, TB, TC, TD, TX, NX)
            IF (IERR .NE. 0) RETURN
            DO 205 I = 1, NX
               L = I + (J-1)*NX + (K-1)*NX*NY
               X(L) = X(L) + OMEGA*(TX(I) - X(L))
  205       CONTINUE
  210    CONTINUE
  220    CONTINUE
C
         RESID  = RESNRM(AE, AW, AN, AS, AT, AB, AD, RHS, X) / R0
         ITUSED = ITER
         IF (RESID .LT. TOL) RETURN
  300 CONTINUE
C
      IWARN = IWARN + 1
      IF (LUNPRT .GT. 0) WRITE (LUNPRT,9200) ITMAX, RESID
      RETURN
 9200 FORMAT (' *** LSOR NOT CONVERGED IN',I5,' ITERATIONS, RESID =',
     &        1PE12.4)
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION RESNRM (AE, AW, AN, AS, AT, AB, AD,
     &                                  RHS, X)
C-----------------------------------------------------------------------
C     L2 NORM OF RHS - A*X OVER ACTIVE CELLS ONLY.
C-----------------------------------------------------------------------
C     --- nativegate: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- nativegate: end INCLUDE 'PETRO.INC' ---
C     --- nativegate: expanded INCLUDE 'GRID.INC' ---
C=======================================================================
C     GRID.INC   -   RESERVOIR GRID AND SOLUTION ARRAYS
C
C     ORIGINALLY WRITTEN  08-JUL-1989   M. OKONKWO / R.T. HALSEY
C     REVISED             22-APR-1992   NX*NY*NZ RAISED TO 40000
C     REVISED             17-OCT-1995   ADDED NON-NEIGHBOUR CONNECTIONS
C
C     STORAGE IS COLUMN-MAJOR NATURAL ORDER:
C         L = I + (J-1)*NX + (K-1)*NX*NY
C     EVERY ROUTINE IN THIS LIBRARY ASSUMES THAT INDEXING.  IF YOU
C     CHANGE IT YOU WILL BREAK TRANX/TRANY/TRANZ AND THE JACOBIAN
C     BANDWIDTH ASSUMPTION IN MATSOL.
C=======================================================================
      PARAMETER (MXCELL = 40000)
      PARAMETER (MXWELL = 200)
      PARAMETER (MXPERF = 50)
      PARAMETER (MXNNC  = 2000)
C
C-----GRID DIMENSIONS ---------------------------------------------------
      COMMON /GDIM/ NX, NY, NZ, NCELL, NACTIV, NNNC
C
C-----STATIC ROCK PROPERTIES -------------------------------------------
      COMMON /ROCK/ PORO(MXCELL), PERMX(MXCELL), PERMY(MXCELL),
     &              PERMZ(MXCELL), DXC(MXCELL), DYC(MXCELL),
     &              DZC(MXCELL), TOPS(MXCELL), NTG(MXCELL)
C
C-----TRANSMISSIBILITIES (BUILT ONCE BY TRNCAL) ------------------------
      COMMON /TRANS/ TRANX(MXCELL), TRANY(MXCELL), TRANZ(MXCELL)
C
C-----PRIMARY UNKNOWNS AND OLD TIME LEVEL ------------------------------
      COMMON /STATE/ PRES(MXCELL), SWAT(MXCELL), SGAS(MXCELL),
     &               POLD(MXCELL), SWOLD(MXCELL), SGOLD(MXCELL)
C
C-----ACTIVE CELL MAP.  IACT(L) = 0 MEANS PINCHED OUT / INACTIVE --------
      COMMON /ACTMAP/ IACT(MXCELL), LNUM(MXCELL)
C
C-----WELL DATA --------------------------------------------------------
C     IWTYPE  1 = PRODUCER (BHP), 2 = PRODUCER (RATE),
C             3 = INJECTOR (BHP), 4 = INJECTOR (RATE)
C
      COMMON /WELLS/ WBHP(MXWELL), WRATE(MXWELL), WWI(MXWELL,MXPERF),
     &               WSKIN(MXWELL), WRAD(MXWELL),
     &               IWCELL(MXWELL,MXPERF), NPERF(MXWELL),
     &               IWTYPE(MXWELL), NWELL
      CHARACTER*8   WNAME
      COMMON /WNAMC/ WNAME(MXWELL)
C
C-----TIME STEPPING ----------------------------------------------------
      COMMON /TSTEP/ TIME, DT, DTMIN, DTMAX, DTFAC, TEND, NSTEP, NCUT
C
C     --- nativegate: end INCLUDE 'GRID.INC' ---
      DIMENSION AE(MXCELL), AW(MXCELL), AN(MXCELL), AS(MXCELL)
      DIMENSION AT(MXCELL), AB(MXCELL), AD(MXCELL)
      DIMENSION RHS(MXCELL), X(MXCELL)
C
      S = ZERO
      DO 420 K = 1, NZ
      DO 410 J = 1, NY
      DO 400 I = 1, NX
         L = I + (J-1)*NX + (K-1)*NX*NY
         IF (IACT(L) .EQ. 0) GO TO 400
         R = RHS(L) - AD(L)*X(L)
         IF (I .GT.  1) R = R + AW(L)*X(L-1)
         IF (I .LT. NX) R = R + AE(L)*X(L+1)
         IF (J .GT.  1) R = R + AS(L)*X(L-NX)
         IF (J .LT. NY) R = R + AN(L)*X(L+NX)
         IF (K .GT.  1) R = R + AB(L)*X(L-NX*NY)
         IF (K .LT. NZ) R = R + AT(L)*X(L+NX*NY)
         S = S + R*R
  400 CONTINUE
  410 CONTINUE
  420 CONTINUE
      RESNRM = DSQRT(S)
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE GAUSSB (A, B, N, LDA, INFO)
C-----------------------------------------------------------------------
C     DENSE GAUSSIAN ELIMINATION WITH PARTIAL PIVOTING.  USED ONLY FOR
C     THE SMALL WELL COUPLING SYSTEM (N .LE. MXWELL) AND FOR THE EOS
C     FLASH JACOBIAN.  A IS OVERWRITTEN, B RETURNS THE SOLUTION.
C-----------------------------------------------------------------------
C     --- nativegate: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- nativegate: end INCLUDE 'PETRO.INC' ---
      DIMENSION A(LDA,N), B(N)
C
      INFO = 0
      DO 540 K = 1, N-1
         IP = K
         AM = DABS(A(K,K))
         DO 500 I = K+1, N
            IF (DABS(A(I,K)) .GT. AM) THEN
               AM = DABS(A(I,K))
               IP = I
            END IF
  500    CONTINUE
         IF (AM .LT. SMALL) THEN
            INFO = K
            RETURN
         END IF
         IF (IP .NE. K) THEN
            DO 510 J = K, N
               T       = A(K,J)
               A(K,J)  = A(IP,J)
               A(IP,J) = T
  510       CONTINUE
            T     = B(K)
            B(K)  = B(IP)
            B(IP) = T
         END IF
         DO 530 I = K+1, N
            FCT = A(I,K) / A(K,K)
            IF (FCT .EQ. ZERO) GO TO 530
            DO 520 J = K+1, N
               A(I,J) = A(I,J) - FCT*A(K,J)
  520       CONTINUE
            B(I) = B(I) - FCT*B(K)
  530    CONTINUE
  540 CONTINUE
C
      IF (DABS(A(N,N)) .LT. SMALL) THEN
         INFO = N
         RETURN
      END IF
      B(N) = B(N) / A(N,N)
      DO 560 I = N-1, 1, -1
         S = B(I)
         DO 550 J = I+1, N
            S = S - A(I,J)*B(J)
  550    CONTINUE
         B(I) = S / A(I,I)
  560 CONTINUE
      RETURN
      END